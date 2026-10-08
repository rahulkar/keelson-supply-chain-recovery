"""Tenant custom specialists and the agent playground (``110-m7-backoffice-generic.md`` §3).

A **custom specialist** is saved into the versioned AgentSpec (``custom_specialists``). The
planner lists it in its catalogue (``description``), the translator shows its
``display_name`` (``SUBAGENT_STARTED``, :func:`keelson_contracts.agui.subagent_started` with
``custom=True``), and it runs the same investigator → structurer sub-workflow as the built-ins
(``Findings`` contract unchanged). Its ``role_prompt`` is admin-authored **data**: the
InstructionProvider appends it as a delimited block after the fixed instruction (never
templated; the numbers rule, tool rules and output schema always win).

Shape checks live on the models; checks against tenant state (approved read tools, aliases,
knowledge bases, no Pro models) are :func:`custom_specialist_violations`. ``untrusted_text``
tools are allowed but always reached through the quarantine wrapper
(:func:`quarantined_tool_refs`); a specialist that combines ``untrusted_text`` with an
external-egress tool is refused (plan §7).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, AwareDatetime, Field, StrictBool, StrictInt, model_validator

from .agui import AGENT_DISPLAY_NAMES
from .common import ContractModel, EvidenceId, NonNegInt
from .connectors import ContentTrust, Egress, ToolAccess, ToolClassification
from .llm import Findings

__all__ = [
    "ALIAS_PATTERN",
    "CUSTOM_SPECIALIST_SLUG_PATTERN",
    "CustomSpecialistSpec",
    "MAX_CUSTOM_SPECIALISTS",
    "MAX_CUSTOM_SPECIALIST_CONNECTORS",
    "MAX_ROLE_PROMPT_CHARS",
    "MAX_SPECIALIST_KBS",
    "MAX_SPECIALIST_TOOLS",
    "PLAYGROUND_MAX_QUESTION_CHARS",
    "PlaygroundError",
    "PlaygroundRequest",
    "PlaygroundResult",
    "PlaygroundStatus",
    "PlaygroundToolCall",
    "PlaygroundUsage",
    "RESERVED_SPECIALIST_SLUGS",
    "SpecialistToolRef",
    "custom_specialist_violations",
    "custom_specialists_violations",
    "is_custom_specialist_slug",
    "is_pro_model",
    "quarantined_tool_refs",
]

MAX_ROLE_PROMPT_CHARS: Final = 4000
MAX_SPECIALIST_TOOLS: Final = 15
"""Plan §7: investigators get at most 15 read tools / calls."""
MAX_SPECIALIST_KBS: Final = 10
MAX_CUSTOM_SPECIALIST_CONNECTORS: Final = 3
MAX_CUSTOM_SPECIALISTS: Final = 8
PLAYGROUND_MAX_QUESTION_CHARS: Final = 2000

CUSTOM_SPECIALIST_SLUG_PATTERN: Final = r"^[a-z][a-z0-9_]{2,39}$"
"""snake_case, 3..40 chars (the engine names agents ``<slug>_investigator``)."""
_SLUG_RE: Final = re.compile(CUSTOM_SPECIALIST_SLUG_PATTERN)

RESERVED_SPECIALIST_SLUGS: Final = frozenset(
    {
        *AGENT_DISPLAY_NAMES,
        "answer",
        "answer_investigator",
        "answer_structurer",
        "investigator",
        "structurer",
        "guard",
        "extractor",
        "followup",
        "system",
        "user",
    }
)
"""Built-in agent roles a custom specialist may not shadow."""

ALIAS_PATTERN: Final = r"^(anthropic/)?[a-z0-9][a-z0-9-]{0,62}$"
"""LiteLLM alias names (``70`` §2; ``anthropic/`` is auto-prefixed for Claude backends)."""
_PRO_RE: Final = re.compile(r"(?:^|[^a-z])pro(?:$|[^a-z])", re.IGNORECASE)
"""``pro`` as its own word: any non-letter (separator, digit, space) or the ends around it
(``gemini-2.5-pro``, ``gemini-1.5-pro002``, ``Gemini 2.5 Pro``; not ``prod`` / ``improve``)."""
_TOOL_NAME_PATTERN: Final = r"^[A-Za-z0-9_.-]{1,128}$"
_CONNECTOR_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,62}$"
_UUID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def is_pro_model(name: str) -> bool:
    """True when an alias or provider model id names a *Pro* tier (``gemini-2.5-pro``,
    ``vertex_ai/gemini-pro``, ``pro-default``): never allowed for tenant specialists (cost)."""
    return bool(_PRO_RE.search(name))


def is_custom_specialist_slug(slug: str) -> bool:
    """A valid custom specialist slug that does not shadow a built-in role."""
    return (
        isinstance(slug, str)
        and _SLUG_RE.fullmatch(slug) is not None
        and slug not in RESERVED_SPECIALIST_SLUGS
        and not slug.endswith(("_investigator", "_structurer"))
        and "__" not in slug
    )


def _slug(value: str) -> str:
    if not is_custom_specialist_slug(value):
        if value in RESERVED_SPECIALIST_SLUGS:
            raise ValueError(f"slug {value!r} is a built-in agent role")
        raise ValueError(
            f"slug must match {CUSTOM_SPECIALIST_SLUG_PATTERN}, contain no '__' and not end in "
            "_investigator/_structurer"
        )
    return value


def _single_line(value: str) -> str:
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError("must be a single line without control characters")
    if value != value.strip():
        raise ValueError("must not start or end with whitespace")
    return value


def _prompt_text(value: str) -> str:
    if any((ord(c) < 0x20 and c not in "\n\t") or ord(c) == 0x7F for c in value):
        raise ValueError("control characters other than newline and tab are not allowed")
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _alias(value: str) -> str:
    if is_pro_model(value):
        raise ValueError("Pro models are not allowed for custom specialists (use a Flash / Flash-Lite alias)")
    return value


CustomSpecialistSlug = Annotated[str, Field(pattern=CUSTOM_SPECIALIST_SLUG_PATTERN), AfterValidator(_slug)]
AliasName = Annotated[str, Field(max_length=80, pattern=ALIAS_PATTERN), AfterValidator(_alias)]


class SpecialistToolRef(ContractModel):
    """One read tool on one connector (the admin-approved ``connector_tool`` row)."""

    connector: Annotated[str, Field(pattern=_CONNECTOR_PATTERN)]
    tool: Annotated[str, Field(pattern=_TOOL_NAME_PATTERN)]

    @property
    def key(self) -> tuple[str, str]:
        return (self.connector, self.tool)


class CustomSpecialistSpec(ContractModel):
    """One tenant-defined specialist (an AgentSpec ``custom_specialists`` entry)."""

    slug: CustomSpecialistSlug
    display_name: Annotated[str, Field(min_length=1, max_length=60), AfterValidator(_single_line)]
    description: Annotated[str, Field(min_length=1, max_length=300), AfterValidator(_single_line)]
    """What it checks, for the planner's catalogue."""
    role_prompt: Annotated[str, Field(min_length=1, max_length=MAX_ROLE_PROMPT_CHARS), AfterValidator(_prompt_text)]
    model_alias: AliasName
    tools: Annotated[list[SpecialistToolRef], Field(max_length=MAX_SPECIALIST_TOOLS)] = Field(default_factory=list)
    kb_ids: Annotated[
        list[Annotated[str, Field(pattern=_UUID_PATTERN)]], Field(max_length=MAX_SPECIALIST_KBS)
    ] = Field(default_factory=list)
    max_tool_calls: Annotated[StrictInt, Field(ge=1, le=MAX_SPECIALIST_TOOLS)] = 8
    max_turns: Annotated[StrictInt, Field(ge=1, le=10)] = 6
    enabled: StrictBool = True

    @model_validator(mode="after")
    def _check(self) -> CustomSpecialistSpec:
        keys = [t.key for t in self.tools]
        if len(set(keys)) != len(keys):
            raise ValueError("tools must be unique")
        if len(set(self.kb_ids)) != len(self.kb_ids):
            raise ValueError("kb_ids must be unique")
        if not self.tools and not self.kb_ids:
            raise ValueError("a specialist needs at least one tool or knowledge base")
        if len({t.connector for t in self.tools}) > MAX_CUSTOM_SPECIALIST_CONNECTORS:
            raise ValueError(f"a specialist reads at most {MAX_CUSTOM_SPECIALIST_CONNECTORS} connectors")
        return self

    @property
    def connectors(self) -> list[str]:
        """Connectors it reads (first-seen order)."""
        return list(dict.fromkeys(t.connector for t in self.tools))


def quarantined_tool_refs(
    spec: CustomSpecialistSpec, catalogue: Mapping[tuple[str, str], ToolClassification]
) -> list[SpecialistToolRef]:
    """Tools the engine must wrap in the quarantine (``untrusted_text``): the investigator
    only ever sees their structured extraction, never raw text."""
    return [
        t for t in spec.tools
        if (c := catalogue.get(t.key)) is not None and c.content_trust is ContentTrust.UNTRUSTED_TEXT
    ]


def custom_specialist_violations(
    spec: CustomSpecialistSpec,
    *,
    catalogue: Mapping[tuple[str, str], ToolClassification],
    aliases: Collection[str],
    kb_ids: Collection[str],
    active_connectors: Collection[str] | None = None,
    alias_models: Mapping[str, str] | None = None,
) -> list[str]:
    """Checks against tenant state (422 on the PUT). ``catalogue`` = the **approved** tools
    ``(connector, tool) → classification``; ``aliases`` = the tenant team's chat aliases;
    ``kb_ids`` = existing knowledge bases; ``active_connectors`` = connectors in status
    ``active`` (``None`` skips the check); ``alias_models`` = alias → provider model (to refuse
    an innocuous alias that points at a Pro model)."""
    problems: list[str] = []
    untrusted = external = False
    for ref in spec.tools:
        cls = catalogue.get(ref.key)
        where = f"{ref.connector}/{ref.tool}"
        if cls is None:
            problems.append(f"{where}: not an approved tool")
            continue
        if cls.access is not ToolAccess.READ:
            problems.append(f"{where}: only read tools may be bound ({cls.access.value})")
        untrusted |= cls.content_trust is ContentTrust.UNTRUSTED_TEXT
        external |= cls.egress is Egress.EXTERNAL
        if active_connectors is not None and ref.connector not in active_connectors:
            problems.append(f"{where}: connector {ref.connector!r} is not active")
    if untrusted and external:
        problems.append("untrusted_text tools cannot be combined with external-egress tools")
    if spec.model_alias not in aliases:
        problems.append(f"model alias {spec.model_alias!r} is not available to this tenant")
    elif alias_models is not None and is_pro_model(alias_models.get(spec.model_alias, "")):
        problems.append(f"model alias {spec.model_alias!r} points at a Pro model")
    if missing := sorted(set(spec.kb_ids) - set(kb_ids)):
        problems.append(f"unknown knowledge bases {missing}")
    return problems


def custom_specialists_violations(specs: Collection[CustomSpecialistSpec]) -> list[str]:
    """List-level checks of ``custom_specialists``: at most :data:`MAX_CUSTOM_SPECIALISTS`,
    unique slugs and display names."""
    problems: list[str] = []
    if len(specs) > MAX_CUSTOM_SPECIALISTS:
        problems.append(f"at most {MAX_CUSTOM_SPECIALISTS} custom specialists")
    slugs = [s.slug for s in specs]
    if len(set(slugs)) != len(slugs):
        problems.append("custom specialist slugs must be unique")
    names = [s.display_name.casefold() for s in specs]
    if len(set(names)) != len(names):
        problems.append("custom specialist display names must be unique")
    return problems


# ──────────────────────────────── playground ────────────────────────────────


class PlaygroundRequest(ContractModel):
    """``POST /api/admin/agents/playground``: one investigator + structurer turn of a saved
    (``slug``) or draft specialist on a scratch case (tenant-scoped, never commits,
    budget-capped). Activity streams as AG-UI events; the answer is a :class:`PlaygroundResult`."""

    specialist: CustomSpecialistSpec | CustomSpecialistSlug
    question: Annotated[str, Field(min_length=1, max_length=PLAYGROUND_MAX_QUESTION_CHARS), AfterValidator(_prompt_text)]


class PlaygroundToolCall(ContractModel):
    connector: Annotated[str, Field(pattern=_CONNECTOR_PATTERN)]
    tool: Annotated[str, Field(pattern=_TOOL_NAME_PATTERN)]
    ok: StrictBool
    duration_ms: NonNegInt
    quarantined: StrictBool
    """Reached through the quarantine wrapper (``untrusted_text`` tool)."""
    evidence_id: EvidenceId | None
    error: Annotated[str, Field(min_length=1, max_length=300)] | None

    @model_validator(mode="after")
    def _ok_xor_error(self) -> PlaygroundToolCall:
        if self.ok == (self.error is not None):
            raise ValueError("a tool call carries an error exactly when it failed")
        if not self.ok and self.evidence_id is not None:
            raise ValueError("a failed tool call has no evidence")
        return self


class PlaygroundUsage(ContractModel):
    llm_calls: NonNegInt
    tool_calls: NonNegInt
    tokens_in: NonNegInt
    tokens_out: NonNegInt


class PlaygroundError(ContractModel):
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    message: Annotated[str, Field(min_length=1, max_length=500)]


PlaygroundStatus = Literal["completed", "failed", "budget_exceeded"]


class PlaygroundResult(ContractModel):
    """The playground turn's outcome. ``committed`` is always false: the scratch case never
    plans or writes."""

    playground_id: Annotated[str, Field(pattern=_UUID_PATTERN)]
    specialist: CustomSpecialistSlug
    display_name: Annotated[str, Field(min_length=1, max_length=60)]
    status: PlaygroundStatus
    findings: Findings | None
    tool_calls: Annotated[list[PlaygroundToolCall], Field(max_length=MAX_SPECIALIST_TOOLS)]
    usage: PlaygroundUsage
    error: PlaygroundError | None
    started_at: AwareDatetime
    duration_ms: NonNegInt
    committed: Literal[False] = False

    @model_validator(mode="after")
    def _consistent(self) -> PlaygroundResult:
        if self.status == "completed":
            if self.findings is None or self.error is not None:
                raise ValueError("a completed turn has findings and no error")
        elif self.error is None:
            raise ValueError(f"a {self.status} turn says why (error)")
        if self.status == "budget_exceeded" and self.error is not None and self.error.code != "budget_exceeded":
            raise ValueError("a budget_exceeded turn has error.code 'budget_exceeded'")
        if self.usage.tool_calls != len(self.tool_calls):
            raise ValueError("usage.tool_calls must equal the number of tool calls")
        return self
