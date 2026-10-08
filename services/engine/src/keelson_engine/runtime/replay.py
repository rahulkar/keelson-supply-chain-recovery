"""Engine-level LLM record/replay (plan §16 "Demo determinism", spike S9).

Every model call of a run can be served from a tenant-scoped Postgres table instead of the live
provider. Unlike the LiteLLM proxy cache (which fails *open*: a miss silently goes live), the
``replay`` mode fails *closed* with :class:`ReplayMiss`, so a demo or an eval run either repeats
a recorded run exactly or stops loudly.

Modes (env ``LLM_REPLAY_MODE``; default ``off`` — production never replays):

* ``off`` — pass-through, zero overhead (no normalisation, no DB access);
* ``record`` — always call live and (re)record the response;
* ``replay`` — serve from the table; a miss raises :class:`ReplayMiss` (no live call);
* ``record_or_replay`` — serve hits, call live on a miss and record it (the dev/demo stack).

Calls outside a bound run (no :class:`~keelson_engine.runtime.context.RunContext`, hence no
tenant) always go live.

**Key** — ``sha256`` of the *normalised* request: the LiteLLM alias and a fingerprint of its
``models.lock`` entry (provider model + params such as ``reasoning_effort``: a recording made with
another model or effort never answers for this one), the calling agent
(``config.labels['adk_agent_name']``) and investigation task, the agent spec version,
the full ``GenerateContentConfig`` (system instruction, tool declarations, output schema,
generation params; ``http_options`` — per-run headers — excluded) and the contents, plus the
occurrence number of that request within the run (an identical request asked twice in one run —
e.g. a retry — gets the second recorded answer, never the first one again).

**Normalisation** — run-volatile tokens become stable placeholders numbered by order of first
appearance in the request: the run/case/invocation/trace ids of the bound run, UUIDs (incl. v7),
``ev_``/``blk_`` ids, 32-hex ids, tool-call ids (``function_call.id`` / ``function_response.id``,
and their mentions in text) and Gemini thought signatures — opaque tokens whose only meaning to a
model is equality, which the first-appearance numbering keeps. The recorded
responses are normalised with the *same* mapping, so on replay every placeholder is re-mapped to
the current run's token at the same position (an evidence id the model cites, a case id in a
summary). Function-call ids the model mints in a response get fresh ids on every replay (unique
within the session). Dates and date-times (``2026-10-07``, ``2026-10-07T09:15:00Z``) are data — an
ETA, a receipt time: order and distance matter, so they are never normalised (a changed one is a
different request; correctness over hit rate). Unknown objects in a request are keyed by their
``repr`` (a value that cannot be keyed misses, it never collides).

**Accounting** — replayed responses report zero usage (the recorded usage and ``cost_usd: 0`` are
in ``custom_metadata['keelson_replay']``); per-run counters (:func:`replay_stats`) keep live,
replayed, recorded and missed calls apart, and each call logs ``llm.replay`` with its outcome.

**Streaming** — the recorded sequence (thought/text partials + final) is stored and re-emitted
quickly on replay; a non-streaming caller only receives the non-partial responses.

**Size** — a record larger than ``LLM_REPLAY_MAX_BYTES`` (default 2 MiB, hard cap 8 MiB = the DB
check) is compacted (partials dropped); still too large → not recorded (``too_large``).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import enum
import hashlib
import json
import os
import re
import uuid
from collections import Counter, OrderedDict
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import BaseModel

from .context import RunContext, current_task, maybe_current_run

__all__ = [
    "DEFAULT_MAX_BYTES",
    "HARD_MAX_BYTES",
    "InMemoryReplayStore",
    "Normaliser",
    "PostgresReplayStore",
    "PreparedCall",
    "REPLAY_MODES",
    "ReplayLlm",
    "ReplayMiss",
    "ReplayMode",
    "ReplayModelFactory",
    "ReplayRecord",
    "ReplayStats",
    "ReplayStore",
    "default_store",
    "alias_fingerprint",
    "generate_with_replay",
    "prepare_call",
    "replay_max_bytes",
    "replay_mode",
    "replay_stats",
    "set_default_store",
]

log = structlog.get_logger(__name__)

ReplayMode = Literal["off", "record", "replay", "record_or_replay"]
REPLAY_MODES: tuple[ReplayMode, ...] = ("off", "record", "replay", "record_or_replay")
KEY_VERSION = 2
"""Bump when the key derivation changes (old records then simply miss). 2: date-times are data,
models.lock alias fingerprint."""
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
HARD_MAX_BYTES = 8 * 1024 * 1024
"""Matches the ``llm_replay.bytes`` check constraint (migration 0006)."""
AGENT_LABEL = "adk_agent_name"
META_KEY = "keelson_replay"


class ReplayMiss(RuntimeError):
    """``replay`` mode found no recorded response for a request (fail closed: no live call)."""

    def __init__(self, *, alias: str, agent: str, key: str, reason: str = "no recorded response") -> None:
        super().__init__(f"LLM replay miss ({reason}) for agent {agent or '?'!r} on alias {alias!r} [key {key[:12]}]")
        self.alias = alias
        self.agent = agent
        self.key = key


def replay_mode(value: str | None = None) -> ReplayMode:
    """The configured mode (``LLM_REPLAY_MODE``; unset/empty → ``off``). Unknown values are a
    configuration error, never a silent fallback."""
    raw = (value if value is not None else os.environ.get("LLM_REPLAY_MODE", "")).strip().lower()
    if not raw:
        return "off"
    if raw not in REPLAY_MODES:
        from .errors import RuntimeConfigError  # noqa: PLC0415

        raise RuntimeConfigError(f"LLM_REPLAY_MODE must be one of {', '.join(REPLAY_MODES)} (got {raw!r})")
    return raw  # type: ignore[return-value]


def replay_max_bytes() -> int:
    raw = os.environ.get("LLM_REPLAY_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_BYTES
    except ValueError:
        value = DEFAULT_MAX_BYTES
    return max(1024, min(value, HARD_MAX_BYTES))


# ─────────────────────────────── normalisation ───────────────────────────────

_PH_OPEN, _PH_CLOSE = "⟦", "⟧"
_PH_RE = re.compile(f"{_PH_OPEN}[^{_PH_OPEN}{_PH_CLOSE}]{{1,80}}{_PH_CLOSE}")
_B = r"(?<![0-9A-Za-z])"
_E = r"(?![0-9A-Za-z])"
_PATTERNS: tuple[tuple[str, str], ...] = (
    ("uuid", rf"{_B}[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}{_E}"),
    ("ev", r"(?<![0-9A-Za-z_])ev_(?=[0-9A-Za-z_-]{0,63}\d)[0-9A-Za-z][0-9A-Za-z_-]{0,63}"),
    ("blk", r"(?<![0-9A-Za-z_])blk_(?=[0-9A-Za-z_-]{0,63}\d)[0-9A-Za-z][0-9A-Za-z_-]{0,63}"),
    ("hex", rf"{_B}[0-9a-f]{{32}}{_E}"),
)
"""Opaque, run-volatile ids only. Never values a model reasons about (numbers, dates, date-times):
two requests that differ in one of those must never share a key."""
_CALL_ID_PARENTS = frozenset({"function_call", "function_response", "functionCall", "functionResponse"})
_SIGNATURE_KEYS = frozenset({"thought_signature", "thoughtSignature"})
_MIN_LITERAL = 6
"""Run literals shorter than this are not normalised (a test case id ``"c"`` is not an id)."""


def _ph(kind: str, n: int | None = None, extra: str = "") -> str:
    body = kind if n is None else f"{kind}#{n}"
    return f"{_PH_OPEN}{body}{':' + extra if extra else ''}{_PH_CLOSE}"


def _literal_regex(literals: list[str]) -> re.Pattern[str] | None:
    if not literals:
        return None
    alts = "|".join(re.escape(s) for s in sorted(set(literals), key=lambda s: (-len(s), s)))
    return re.compile(f"{_B}(?:{alts}){_E}")


class Normaliser:
    """Concrete token ↔ placeholder mapping for one request (numbered by first appearance)."""

    def __init__(self, literals: Iterable[tuple[str, str]] = ()) -> None:
        self.forward: dict[str, str] = {}
        """concrete → placeholder"""
        self._counts: Counter[str] = Counter()
        self._literals: list[str] = []
        for concrete, kind in literals:
            if concrete and len(concrete) >= _MIN_LITERAL and concrete not in self.forward:
                self.forward[concrete] = _ph(kind)
                self._literals.append(concrete)
        self._scanner: re.Pattern[str] | None = None

    @classmethod
    def for_run(cls, run: RunContext | None) -> Normaliser:
        if run is None:
            return cls()
        # longest first: ``e-<run id>`` (invocation) before the bare run id
        return cls([(run.invocation_id, "inv"), (run.run_id, "run"), (run.case_id, "case"), (run.trace_id, "trace")])

    def _assign(self, kind: str, concrete: str) -> str:
        placeholder = self.forward.get(concrete)
        if placeholder is None:
            self._counts[kind] += 1
            placeholder = _ph(kind, self._counts[kind])
            self.forward[concrete] = placeholder
        return placeholder

    # request ------------------------------------------------------------------------------

    def normalise_request(self, doc: Any) -> Any:
        """Pass 1 numbers the structural tokens (call ids, signatures) in traversal order; pass 2
        replaces them and every pattern match in every string (dict keys included)."""
        self._collect(doc, None)
        literal_alt = "|".join(
            re.escape(s) for s in sorted(set(self._literals), key=lambda s: (-len(s), s))
        )
        parts = [f"(?P<lit>{_B}(?:{literal_alt}){_E})"] if literal_alt else []
        parts += [f"(?P<{kind}>{pattern})" for kind, pattern in _PATTERNS]
        self._scanner = re.compile("|".join(parts))
        return self._rewrite_request(doc, None)

    def _collect(self, obj: Any, parent: str | None) -> None:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                if k in _SIGNATURE_KEYS and isinstance(v, str) and v:
                    self._assign("sig", v)
                elif k == "id" and parent in _CALL_ID_PARENTS and isinstance(v, str) and v:
                    if v not in self.forward:
                        self._assign("call", v)
                        self._literals.append(v)
                else:
                    self._collect(v, k if isinstance(k, str) else None)
        elif isinstance(obj, list):
            for item in obj:
                self._collect(item, parent)

    def _scan(self, text: str) -> str:
        assert self._scanner is not None

        def repl(m: re.Match[str]) -> str:
            kind = m.lastgroup or "x"
            token = m.group(0)
            if kind == "lit":
                return self.forward[token]
            return self._assign(kind, token)

        return self._scanner.sub(repl, text)

    def _rewrite_request(self, obj: Any, parent: str | None) -> Any:
        if isinstance(obj, str):
            return self._scan(obj)
        if isinstance(obj, Mapping):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                key = self._scan(k) if isinstance(k, str) else k
                if k in _SIGNATURE_KEYS and isinstance(v, str) and v:
                    out[key] = self.forward[v]
                elif k == "id" and parent in _CALL_ID_PARENTS and isinstance(v, str) and v:
                    out[key] = self.forward[v]
                else:
                    out[key] = self._rewrite_request(v, k if isinstance(k, str) else None)
            return out
        if isinstance(obj, list):
            return [self._rewrite_request(v, parent) for v in obj]
        return obj

    # responses ----------------------------------------------------------------------------

    def normalise_response(self, doc: Any) -> Any:
        """Replace tokens *known from the request* with their placeholders; model-minted call ids
        become ``newcall`` placeholders (fresh ids on replay). Unknown tokens stay literal."""
        known = _literal_regex([k for k, ph in self.forward.items() if not ph.startswith(_PH_OPEN + "sig")])
        fresh: dict[str, str] = {}

        def text(s: str) -> str:
            return known.sub(lambda m: self.forward[m.group(0)], s) if known is not None else s

        def walk(obj: Any, parent: str | None) -> Any:
            if isinstance(obj, str):
                return text(obj)
            if isinstance(obj, Mapping):
                out: dict[str, Any] = {}
                for k, v in obj.items():
                    if k in _SIGNATURE_KEYS:
                        out[k] = v  # opaque provider bytes: kept as recorded
                    elif k == "id" and parent in _CALL_ID_PARENTS and isinstance(v, str) and v:
                        if v in self.forward:
                            out[k] = self.forward[v]
                        else:
                            if v not in fresh:
                                prefix = re.match(r"^[A-Za-z]{1,16}[-_]", v)
                                fresh[v] = _ph("newcall", len(fresh) + 1, prefix.group(0) if prefix else "")
                            out[k] = fresh[v]
                    else:
                        out[text(k) if isinstance(k, str) else k] = walk(v, k if isinstance(k, str) else None)
                return out
            if isinstance(obj, list):
                return [walk(v, parent) for v in obj]
            return obj

        return walk(doc, None)

    def denormalise(self, doc: Any, fresh: dict[str, str] | None = None) -> Any:
        """Placeholders → this request's concrete tokens; ``newcall`` → fresh ids (shared through
        ``fresh`` across the responses of one replayed call). Unknown placeholders stay as-is."""
        reverse = {ph: concrete for concrete, ph in self.forward.items()}
        fresh = {} if fresh is None else fresh

        def repl(m: re.Match[str]) -> str:
            ph = m.group(0)
            if ph in reverse:
                return reverse[ph]
            if ph.startswith(_PH_OPEN + "newcall#"):
                if ph not in fresh:
                    body = ph[1:-1]
                    prefix = body.split(":", 1)[1] if ":" in body else ""
                    fresh[ph] = f"{prefix}{uuid.uuid4().hex}"
                return fresh[ph]
            return ph

        def walk(obj: Any) -> Any:
            if isinstance(obj, str):
                return _PH_RE.sub(repl, obj) if _PH_OPEN in obj else obj
            if isinstance(obj, Mapping):
                return {(walk(k) if isinstance(k, str) else k): walk(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [walk(v) for v in obj]
            return obj

        return walk(doc)


def _plain(obj: Any) -> Any:
    """Deterministic JSON-able view of request objects (pydantic models field by field, output
    schema classes as their JSON schema, bytes as base64, callables by qualified name)."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, enum.Enum):
        return _plain(obj.value)
    if isinstance(obj, type):
        if issubclass(obj, BaseModel):
            return {"$model": obj.__qualname__, "schema": obj.model_json_schema()}
        return f"{obj.__module__}.{obj.__qualname__}"
    if isinstance(obj, BaseModel):
        out: dict[str, Any] = {}
        for name in type(obj).model_fields:
            value = getattr(obj, name, None)
            if value is not None:
                out[name] = _plain(value)
        return out
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_plain(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if callable(obj):
        return f"{getattr(obj, '__module__', '')}.{getattr(obj, '__qualname__', type(obj).__qualname__)}"
    # never just the type name: two different values must not share a key (a repr with an address
    # makes the request unkeyable — it misses, which is the safe direction)
    return f"<{type(obj).__qualname__}:{obj!r}>"


_LOCK_CACHE: dict[str, tuple[int, dict[str, str]]] = {}


def alias_fingerprint(alias: str) -> str:
    """``sha256`` (16 hex) of the alias's ``models.lock`` entry (``MODELS_LOCK``, default
    ``/app/models.lock``), ``""`` when there is no readable lock file (tests, tools). The proxy
    resolves the alias, so the alias alone would let a recording made with another provider model or
    reasoning effort answer for this one."""
    path = os.environ.get("MODELS_LOCK", "").strip() or "/app/models.lock"
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return ""
    cached = _LOCK_CACHE.get(path)
    if cached is None or cached[0] != mtime:
        try:
            import yaml  # noqa: PLC0415

            data = yaml.safe_load(open(path, encoding="utf-8").read()) or {}  # noqa: SIM115
            aliases = data.get("aliases") if isinstance(data, Mapping) else None
        except Exception as exc:  # noqa: BLE001 - an unreadable lock only means "no fingerprint"
            log.warning("llm.replay models.lock unreadable", path=path, error=type(exc).__name__)
            aliases = None
        prints = {
            str(name): hashlib.sha256(_canonical(_plain(entry)).encode("utf-8")).hexdigest()[:16]
            for name, entry in (aliases or {}).items()
        } if isinstance(aliases, Mapping) else {}
        cached = _LOCK_CACHE[path] = (mtime, prints)
    return cached[1].get(alias, "")


_CONFIG_EXCLUDED = frozenset({"http_options"})
"""Per-run transport details (LiteLLM trace/customer headers) — never part of the key."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class PreparedCall:
    key: str
    base_key: str
    occurrence: int
    alias: str
    agent: str
    spec_version: str
    normaliser: Normaliser


def prepare_call(llm_request: LlmRequest, *, alias: str, run: RunContext | None, occurrence: int | None = None
                 ) -> PreparedCall:
    """Normalise the request and derive its key. ``occurrence=None`` takes the next occurrence
    of this request in the run (:class:`ReplayStats`)."""
    config = llm_request.config
    labels = dict(getattr(config, "labels", None) or {}) if config is not None else {}
    agent = str(labels.get(AGENT_LABEL) or "")
    config_doc: dict[str, Any] = {}
    if config is not None:
        for name in type(config).model_fields:
            if name in _CONFIG_EXCLUDED:
                continue
            value = getattr(config, name, None)
            if value is not None:
                config_doc[name] = _plain(value)
    task = current_task()
    spec_version = str(getattr(getattr(run, "spec", None), "version", "") or "") if run is not None else ""
    doc = {
        "v": KEY_VERSION,
        "alias": alias,
        "alias_lock": alias_fingerprint(alias),
        "agent": agent,
        "task": task.key if task is not None else None,
        "spec_version": spec_version,
        "config": config_doc,
        "contents": [_plain(c) for c in llm_request.contents or []],
    }
    normaliser = Normaliser.for_run(run)
    normalised = normaliser.normalise_request(doc)
    base_key = hashlib.sha256(_canonical(normalised).encode("utf-8")).hexdigest()
    if occurrence is None:
        occurrence = replay_stats(run.run_id).next_occurrence(base_key) if run is not None else 0
    key = hashlib.sha256(f"{base_key}:{occurrence}".encode()).hexdigest()
    return PreparedCall(key=key, base_key=base_key, occurrence=occurrence, alias=alias, agent=agent,
                        spec_version=spec_version, normaliser=normaliser)


# ─────────────────────────────── records & stores ───────────────────────────────


@dataclass(frozen=True)
class ReplayRecord:
    key: str
    alias: str
    agent: str
    spec_version: str
    model: str
    """Provider model id that produced the response (``model_version``), else the alias."""
    responses: list[dict[str, Any]]
    """Normalised ``LlmResponse`` dumps in emission order (partials + final)."""
    usage: dict[str, Any] | None = None
    bytes: int = 0
    created_at: dt.datetime | None = None


class ReplayStore(Protocol):
    async def get(self, *, tenant: str, key: str) -> ReplayRecord | None: ...

    async def put(self, *, tenant: str, record: ReplayRecord) -> None: ...


class InMemoryReplayStore:
    """Process-local store (tests, offline evals). Tenant-partitioned like the table."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], ReplayRecord] = {}
        self.gets = 0
        self.puts = 0

    async def get(self, *, tenant: str, key: str) -> ReplayRecord | None:
        self.gets += 1
        return self.records.get((tenant, key))

    async def put(self, *, tenant: str, record: ReplayRecord) -> None:
        if record.bytes > HARD_MAX_BYTES:
            raise ValueError("replay record too large")
        self.puts += 1
        self.records[(tenant, record.key)] = record


class PostgresReplayStore:
    """The ``llm_replay`` table (migration 0006; RLS by tenant) through ``core.repo``."""

    async def get(self, *, tenant: str, key: str) -> ReplayRecord | None:
        from keelson_engine.core import repo  # noqa: PLC0415 - runtime never imports core eagerly

        row = await repo.get_llm_replay(key, tenant=tenant)
        if row is None:
            return None
        responses = row["responses"]
        if isinstance(responses, str):
            responses = json.loads(responses)
        usage = row.get("usage")
        if isinstance(usage, str):
            usage = json.loads(usage)
        return ReplayRecord(
            key=row["key"], alias=row["alias"], agent=row["agent"], spec_version=row["spec_version"],
            model=row["model"], responses=list(responses), usage=usage, bytes=int(row["bytes"]),
            created_at=row.get("created_at"),
        )

    async def put(self, *, tenant: str, record: ReplayRecord) -> None:
        from keelson_engine.core import repo  # noqa: PLC0415

        await repo.put_llm_replay(
            record.key, alias=record.alias, agent=record.agent, spec_version=record.spec_version,
            model=record.model, responses=record.responses, usage=record.usage, size=record.bytes, tenant=tenant,
        )


_default_store: ReplayStore | None = None


def default_store() -> ReplayStore:
    global _default_store
    if _default_store is None:
        _default_store = PostgresReplayStore()
    return _default_store


def set_default_store(store: ReplayStore | None) -> None:
    """Install a store for every model without an explicit one (``None`` → Postgres again)."""
    global _default_store
    _default_store = store


# ─────────────────────────────── per-run accounting ───────────────────────────────


@dataclass
class ReplayStats:
    """Per-run counters: replayed calls are counted apart from live (billed) calls."""

    live: int = 0
    replayed: int = 0
    recorded: int = 0
    misses: int = 0
    too_large: int = 0
    store_errors: int = 0
    replayed_tokens: int = 0
    """Tokens the recorded responses used (not spent again)."""
    occurrences: Counter[str] = field(default_factory=Counter)

    def next_occurrence(self, base_key: str) -> int:
        n = self.occurrences[base_key]
        self.occurrences[base_key] = n + 1
        return n

    def as_dict(self) -> dict[str, int]:
        return {"live": self.live, "replayed": self.replayed, "recorded": self.recorded, "misses": self.misses,
                "too_large": self.too_large, "store_errors": self.store_errors,
                "replayed_tokens": self.replayed_tokens}


_RUN_STATS: OrderedDict[str, ReplayStats] = OrderedDict()
_MAX_TRACKED_RUNS = 512


def replay_stats(run_id: str) -> ReplayStats:
    stats = _RUN_STATS.get(run_id)
    if stats is None:
        stats = _RUN_STATS[run_id] = ReplayStats()
        while len(_RUN_STATS) > _MAX_TRACKED_RUNS:
            _RUN_STATS.popitem(last=False)
    else:
        _RUN_STATS.move_to_end(run_id)
    return stats


# ─────────────────────────────── record / replay ───────────────────────────────


_NOT_RECORDED = frozenset({"custom_metadata", "live_session_id", "interaction_id", "environment_id"})
"""Response fields never stored: metadata other layers may attach (headers, ids of a live session)
is not the model's answer and must not end up in a long-lived table."""


def _dump(response: LlmResponse) -> dict[str, Any]:
    return response.model_dump(mode="json", exclude_none=True, exclude=set(_NOT_RECORDED))


def _load(doc: Mapping[str, Any]) -> LlmResponse:
    # JSON validation: bytes (thought signatures) round-trip through base64
    return LlmResponse.model_validate_json(json.dumps(doc))


def _usage_of(responses: list[LlmResponse]) -> dict[str, Any] | None:
    for response in reversed(responses):
        if response.usage_metadata is not None:
            return response.usage_metadata.model_dump(mode="json", exclude_none=True)
    return None


def build_record(call: PreparedCall, responses: list[LlmResponse], *, max_bytes: int) -> ReplayRecord | None:
    """Normalised record of a completed live call, or ``None`` when it is too large."""
    model = next((r.model_version for r in reversed(responses) if r.model_version), None) or call.alias
    docs = [call.normaliser.normalise_response(_dump(r)) for r in responses]
    for candidate in (docs, [d for d in docs if not d.get("partial")]):
        size = len(_canonical(candidate).encode("utf-8"))
        if size <= max_bytes:
            return ReplayRecord(key=call.key, alias=call.alias, agent=call.agent, spec_version=call.spec_version,
                                model=model, responses=candidate, usage=_usage_of(responses), bytes=size)
    return None


def replay_responses(record: ReplayRecord, call: PreparedCall, *, stream: bool) -> list[LlmResponse]:
    fresh: dict[str, str] = {}
    out: list[LlmResponse] = []
    for doc in record.responses:
        partial = bool(doc.get("partial"))
        if partial and not stream:
            continue
        restored = dict(call.normaliser.denormalise(doc, fresh))
        if not partial:
            restored["usage_metadata"] = {"prompt_token_count": 0, "candidates_token_count": 0, "total_token_count": 0}
            meta = dict(restored.get("custom_metadata") or {})
            meta[META_KEY] = {"replayed": True, "key": record.key[:16], "model": record.model, "cost_usd": 0.0,
                              "recorded_usage": record.usage or {}}
            restored["custom_metadata"] = meta
        out.append(_load(restored))
    return out


def _recordable(responses: list[LlmResponse]) -> bool:
    return any(not r.partial for r in responses) and not any(r.error_code for r in responses)


LiveCall = Callable[[], AsyncGenerator[LlmResponse]]


async def generate_with_replay(
    *,
    live: LiveCall,
    llm_request: LlmRequest,
    stream: bool,
    alias: str,
    mode: ReplayMode | None = None,
    store: ReplayStore | None = None,
) -> AsyncGenerator[LlmResponse]:
    """Serve one model call according to ``mode`` (default: ``LLM_REPLAY_MODE``)."""
    mode = replay_mode() if mode is None else replay_mode(mode)
    run = maybe_current_run()
    if mode == "off" or run is None:
        async with aclosing(live()) as stream_:
            async for response in stream_:
                yield response
        return

    store = store or default_store()
    call = prepare_call(llm_request, alias=alias, run=run)
    stats = replay_stats(run.run_id)
    fields = {"alias": alias, "agent": call.agent, "key": call.key[:16], "run_id": run.run_id, "mode": mode}

    if mode in ("replay", "record_or_replay"):
        try:
            record = await store.get(tenant=run.tenant, key=call.key)
        except Exception as exc:  # noqa: BLE001 - the store must never break a record_or_replay run
            stats.store_errors += 1
            if mode == "replay":
                # the exception class only: a driver message can carry SQL parameters (the payload)
                raise ReplayMiss(alias=alias, agent=call.agent, key=call.key,
                                 reason=f"store error: {type(exc).__name__}") from exc
            log.warning("llm.replay", outcome="store_error", error=type(exc).__name__, **fields)
            record = None
        if record is not None:
            responses = replay_responses(record, call, stream=stream)
            stats.replayed += 1
            stats.replayed_tokens += int((record.usage or {}).get("total_token_count") or 0)
            log.info("llm.replay", outcome="hit", cost_usd=0.0, recorded_tokens=(record.usage or {}).get(
                "total_token_count"), model=record.model, **fields)
            for response in responses:
                yield response
                await asyncio.sleep(0)
            return
        stats.misses += 1
        if mode == "replay":
            log.warning("llm.replay", outcome="miss", **fields)
            raise ReplayMiss(alias=alias, agent=call.agent, key=call.key)

    stats.live += 1
    collected: list[LlmResponse] = []
    async with aclosing(live()) as stream_:
        async for response in stream_:
            collected.append(response)
            yield response
    # only reached when the caller consumed the whole call (an abandoned call is never recorded)
    if not _recordable(collected):
        log.info("llm.replay", outcome="not_recorded", **fields)
        return
    record = build_record(call, collected, max_bytes=replay_max_bytes())
    if record is None:
        stats.too_large += 1
        log.warning("llm.replay", outcome="too_large", max_bytes=replay_max_bytes(), **fields)
        return
    try:
        await store.put(tenant=run.tenant, record=record)
    except Exception as exc:  # noqa: BLE001 - recording is best effort; the live answer stands
        stats.store_errors += 1
        log.warning("llm.replay", outcome="record_failed", error=type(exc).__name__, **fields)
        return
    stats.recorded += 1
    log.info("llm.replay", outcome="recorded", bytes=record.bytes, **fields)


# ─────────────────────────────── generic wrapper ───────────────────────────────


class ReplayLlm(BaseLlm):
    """Record/replay around any model (offline evals, tests): ``inner`` answers live calls."""

    model: str = "replay"
    inner: Any
    """The wrapped :class:`BaseLlm` (typed ``Any`` for pydantic)."""
    alias: str
    mode: str | None = None
    store: Any = None

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        inner: BaseLlm = self.inner
        async for response in generate_with_replay(
            live=lambda: inner.generate_content_async(llm_request, stream=stream),
            llm_request=llm_request, stream=stream, alias=self.alias,
            mode=self.mode, store=self.store,  # type: ignore[arg-type]
        ):
            yield response


class ReplayModelFactory:
    """Wraps a model factory so every model it builds records/replays."""

    def __init__(self, inner: Callable[..., BaseLlm], *, mode: ReplayMode | None = None,
                 store: ReplayStore | None = None) -> None:
        self.inner = inner
        self.mode = mode
        self.store = store

    def __call__(self, *, role: str, alias: str) -> BaseLlm:
        model = self.inner(role=role, alias=alias)
        return ReplayLlm(inner=model, alias=alias, mode=self.mode, store=self.store, model=f"replay/{alias}")
