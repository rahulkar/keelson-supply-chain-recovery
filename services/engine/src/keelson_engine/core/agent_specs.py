"""Versioned agent specs (``docs/contracts/70-backoffice-m2.md`` §1).

Rows: ``agent_spec(tenant, version 'v<n>', seq n, spec jsonb, created_by, created_at, summary,
base_version)``. The stored ``spec`` is the **full** spec (``AgentSpec`` minus ``tenant`` /
``version``, keys in model order) so two versions diff cleanly. Without any row the tenant runs
the built-in default (``version = "default"``). Runs pin the latest version at enqueue
(``core.runs``).

``PUT`` = RFC 7386 merge patch on the latest spec with optimistic concurrency (``base_version``
must be the latest, else 409). Validation (422 unless noted):

* structure: the runtime ``AgentSpec`` model (``extra="forbid"``) + prompt addenda ≤ 4,000
  chars for ``intake``, ``answer_investigator``, ``investigator``, ``structurer``,
  ``recommender``, ``planner``, ``critic`` (M4);
* everything that differs from the latest version: model aliases must be chat aliases of the
  tenant team (tool-using roles need tool-loop signatures); added tools must be ``approved`` +
  ``read`` + ``trusted_data`` on the bound, **active** connector;
* M4 quarantine sources (``SpecialistSpec.quarantined``) that a specialist adds (all of them when
  its connector changes): a connector source (``supplier_messages`` / ``events``) needs its MCP
  tool (:data:`QUARANTINE_SOURCE_TOOLS`) ``approved`` + ``read`` + ``untrusted_text`` on the
  specialist's bound, active connector; ``knowledge_base`` needs an existing knowledge base:
  every id of ``investigation.kb_ids`` (checked whenever it changes, too), or at least one of the
  tenant's when it is ``null`` (= all) → 422 ``quarantine_source_invalid``;
* budgets ≤ platform maxima;
* raising ``policy.auto_max_lines`` / ``policy.auto_max_units`` → 403 ``guarded_change`` unless
  platform-admin; likewise changing ``policy.approver_role``, lowering ``policy.approval_quorum``
  and changing the planning ``connector`` (which must then be an existing, active connector);
* M5: any change of the effective approval rules that ``core.policies.guarded_changes`` flags
  (new / widened auto rules, removed / narrowed / weakened refuse and approval rules — also when
  they come from the default caps such as ``policy.four_eyes_cost_cents``) → 403
  ``guarded_change`` with ``reasons`` unless platform-admin.
* M7 (four-eyes, ``110`` §1): instead of the 403 above, a tenant-admin's guarded change (once
  everything else validates) raises ``change_requests.GuardedChange`` → ``202`` + a pending change
  request (kind ``agent_spec``, or ``policy_set`` via ``PUT /policies``; ``base_version`` = the spec
  version it was made against). Approval re-runs :func:`put_spec` / :func:`restore` with
  ``cosigned=True`` (409 when the spec moved).
"""

from __future__ import annotations

import copy
import datetime as dt
import re
import uuid
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from keelson_contracts.connectors import ContentTrust, ToolAccess
from keelson_engine.config import get_settings
from keelson_engine.core import connectors as reg
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = [
    "DEFAULT_VERSION",
    "GUARDED_FIELDS",
    "PROMPT_MAX",
    "PROMPT_ROLES",
    "QUARANTINE_KB_SOURCES",
    "QUARANTINE_SOURCE_TOOLS",
    "aliases_in_use",
    "bound_connectors",
    "catalog",
    "create_initial",
    "custom_specialist_problems",
    "default_spec_dict",
    "get_version",
    "latest",
    "latest_version_name",
    "list_versions",
    "merge_patch",
    "put_spec",
    "reset_to_default",
    "restore",
    "set_spec_model",
    "upgrade_builtins",
    "validate_custom_specialists",
]

log = get_logger(__name__)

DEFAULT_VERSION = "default"
PROMPT_ROLES = ("intake", "answer_investigator", "investigator", "structurer", "recommender", "planner", "critic")
"""Roles with a tenant prompt addendum (the ``runtime.spec.PromptAddenda`` fields)."""
QUARANTINE_SOURCE_TOOLS = {"supplier_messages": "list_supplier_messages", "events": "list_events",
                           "signal_hub": "search_signals"}
"""Connector quarantine source → the MCP tool its (first) wrapper calls deterministically
(``runtime.quarantine.QUARANTINE_SOURCES[...].source_tool``; a runtime test keeps them in step).
``signal_hub`` (M6) is the internal Signal Hub connector: its ``search_signals`` /
``get_event`` / ``hazards_near_node`` tools are provisioned ``untrusted_text``."""
QUARANTINE_KB_SOURCES = frozenset({"knowledge_base"})
"""Quarantine sources read from the tenant's knowledge bases (``core.kb.search``)."""
PROMPT_MAX = 4000
TOOL_ROLES = frozenset({"answer_investigator", "investigator"})
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ROLE_DISPLAY = {
    "intake": "Intake",
    "answer_investigator": "Answer investigator",
    "answer_structurer": "Answer structurer",
    "investigator": "Specialist investigators",
    "structurer": "Specialist structurers",
    "recommender": "Strategist",
    "planner": "Planner",
    "critic": "Critic",
    "guard": "Injection guard",
    "extractor": "Quarantined extractor",
    "followup": "Follow-up classifier",
}
ROLE_PROMPT = {r: r for r in PROMPT_ROLES}
SPECIALIST_DISPLAY = {
    "inventory_demand": "Inventory & demand analyst",
    "sourcing": "Sourcing analyst",
    "logistics": "Logistics analyst",
    "playbook": "Playbook analyst",
}
LEGACY_DEFAULT_BUDGETS = ({"max_llm_calls": 40, "max_total_tokens": 600_000},)
"""Built-in budget defaults of earlier releases: a tenant still on one of them gets the current
default when :func:`upgrade_builtins` adds specialists (more agents need a larger run budget)."""
GUARDED_POLICY = ("auto_max_lines", "auto_max_units")
GUARDED_FIELDS = (*(f"policy.{f}" for f in GUARDED_POLICY), "policy.approver_role", "policy.approval_quorum",
                  "connector")
"""Fields a tenant-admin may not loosen (403 ``guarded_change``): raising the auto-approval
thresholds (contract 70 §1), plus — same rationale — changing who approves change sets, lowering
the approval quorum and re-pointing the planning connector (where change sets are written)."""

_spec_model: type[BaseModel] | None = None


def set_spec_model(model: type[BaseModel] | None) -> None:
    """Tests: validate against another ``AgentSpec`` model (default: the runtime's)."""
    global _spec_model
    _spec_model = model


def _model() -> type[BaseModel]:
    if _spec_model is not None:
        return _spec_model
    from keelson_engine.runtime.spec import AgentSpec

    return AgentSpec


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude={"tenant", "version"})


def default_spec_dict(tenant: str) -> dict[str, Any]:
    vendor = get_settings().brand_vendor_name
    return _dump(_model()(tenant=tenant, vendor_name=vendor))


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch (objects merge, ``null`` removes → model default, arrays replace)."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def _check_m2_fields(data: dict[str, Any]) -> None:
    prompts = data.get("prompts")
    if prompts is not None:
        if not isinstance(prompts, dict):
            raise AdminError(422, "spec_invalid", "prompts must be an object", errors=[{"loc": "prompts"}])
        for role, value in prompts.items():
            if role not in PROMPT_ROLES:
                raise AdminError(422, "spec_invalid", f"unknown prompt role {role!r}",
                                 errors=[{"loc": f"prompts.{role}", "msg": "unknown role"}])
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > PROMPT_MAX or "\x00" in value:
                raise AdminError(422, "spec_invalid", f"the {role} addendum must be text of at most {PROMPT_MAX} "
                                 "characters", errors=[{"loc": f"prompts.{role}", "msg": "too long"}])
    for i, sp in enumerate(data.get("specialists") or []):
        if isinstance(sp, dict):
            if sp.get("connector") is not None and not SLUG_RE.match(str(sp["connector"])):
                raise AdminError(422, "spec_invalid", "invalid specialist connector",
                                 errors=[{"loc": f"specialists.{i}.connector"}])
            if sp.get("display_name") is not None and (not isinstance(sp["display_name"], str)
                                                       or len(sp["display_name"]) > 80):
                raise AdminError(422, "spec_invalid", "display_name must be at most 80 characters",
                                 errors=[{"loc": f"specialists.{i}.display_name"}])
    if data.get("answer_connector") is not None and not SLUG_RE.match(str(data["answer_connector"])):
        raise AdminError(422, "spec_invalid", "invalid answer_connector", errors=[{"loc": "answer_connector"}])


def normalize(tenant: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate a full spec dict with the runtime model → canonical full spec dict."""
    if not isinstance(data, dict):
        raise AdminError(422, "spec_invalid", "spec must be an object")
    _check_m2_fields(data)
    payload = {k: v for k, v in data.items() if k not in ("tenant", "version")}
    try:
        model = _model().model_validate({**payload, "tenant": tenant, "version": "candidate"})
    except ValidationError as exc:
        errors = [{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", ""))[:200]}
                  for e in exc.errors()[:20]]
        raise AdminError(422, "spec_invalid", "the agent spec is invalid", errors=errors) from exc
    return _dump(model)


# ───────────────────────────── reads ─────────────────────────────

_ROW_COLS = "version, seq, spec, created_by, created_at, summary, base_version"
_LATEST_SQL = f"SELECT {_ROW_COLS} FROM agent_spec ORDER BY seq DESC NULLS LAST, created_at DESC LIMIT 1"


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else None


async def _latest_row(s: AsyncSession) -> dict[str, Any] | None:
    m = (await s.execute(text(_LATEST_SQL))).mappings().first()
    return dict(m) if m is not None else None


def _envelope(row: dict[str, Any] | None, tenant: str, *, active: bool) -> dict[str, Any]:
    if row is None:
        return {"version": DEFAULT_VERSION, "spec": default_spec_dict(tenant), "created_by": None,
                "created_at": None, "summary": "built-in default", "active": active}
    return {"version": row["version"], "spec": row["spec"], "created_by": row["created_by"],
            "created_at": _iso(row["created_at"]), "summary": row.get("summary"), "active": active}


async def latest(tenant: str) -> dict[str, Any]:
    async with tenant_session(tenant) as s:
        row = await _latest_row(s)
    return _envelope(row, tenant, active=True)


async def latest_version_name(tenant: str) -> str:
    async with tenant_session(tenant) as s:
        row = await _latest_row(s)
    return row["version"] if row else DEFAULT_VERSION


async def list_versions(tenant: str) -> list[dict[str, Any]]:
    async with tenant_session(tenant) as s:
        res = await s.execute(text(
            "SELECT version, created_by, created_at, summary FROM agent_spec "
            "ORDER BY seq DESC NULLS LAST, created_at DESC"))
        rows = res.mappings().all()
    return [{"version": r["version"], "created_by": r["created_by"], "created_at": _iso(r["created_at"]),
             "summary": r["summary"]} for r in rows]


async def get_version(tenant: str, version: str) -> dict[str, Any]:
    if version == DEFAULT_VERSION:
        current = await latest_version_name(tenant)
        return _envelope(None, tenant, active=current == DEFAULT_VERSION)
    async with tenant_session(tenant) as s:
        m = (await s.execute(text(f"SELECT {_ROW_COLS} FROM agent_spec WHERE version = :v"),
                             {"v": version})).mappings().first()
        latest_row = await _latest_row(s)
    if m is None:
        raise AdminError(404, "not_found", "spec version not found")
    return _envelope(dict(m), tenant, active=latest_row is not None and latest_row["version"] == version)


# ───────────────────────────── diffs / semantics ─────────────────────────────


def diff_paths(a: Any, b: Any, prefix: str = "", depth: int = 3) -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict) and depth > 0:
        out: list[str] = []
        for k in list(dict.fromkeys([*a.keys(), *b.keys()])):
            if a.get(k) != b.get(k):
                out.extend(diff_paths(a.get(k), b.get(k), f"{prefix}{k}.", depth - 1))
        return out
    return [prefix.rstrip(".")] if a != b else []


def _summary(paths: list[str]) -> str:
    text_ = ", ".join(paths) if paths else "no changes"
    return text_ if len(text_) <= 300 else text_[:297] + "…"


def _policy_rule_guards(base_policy: dict[str, Any], new_policy: dict[str, Any]) -> list[dict[str, str]]:
    """M5 policy rules (``core.policies.guarded_changes``) between the effective rule sets of the
    two ``policy`` sections (explicit ``rules`` or the defaults built from the caps)."""
    from keelson_engine.core import policies

    if base_policy == new_policy:
        return []
    try:
        old_set, _ = policies.effective_policy(base_policy)
    except ValidationError:  # an invalid stored rule set: compare against the defaults
        old_set = policies.DEFAULT_POLICY
    new_set, _ = policies.effective_policy(new_policy)
    return policies.guarded_changes(old_set, new_set)


_LEGACY_GUARD_CODES = {
    "policy.auto_max_lines": ("auto_cap_raised", "raises the automatic-approval cap on lines"),
    "policy.auto_max_units": ("auto_cap_raised", "raises the automatic-approval cap on units"),
    "policy.approver_role": ("approver_changed", "changes who approves change sets"),
    "policy.approval_quorum": ("quorum_lowered", "lowers the number of approvals a change set needs"),
    "connector": ("planning_connector_changed", "re-points the connector change sets are written to"),
}


def spec_guard_reasons(fields: list[str], rule_guards: list[dict[str, str]]) -> list[dict[str, str]]:
    """``GuardReason`` dicts (``{code, field, reason}``) for the guarded spec fields + policy rules."""
    out: list[dict[str, str]] = []
    for f in fields:
        if f in _LEGACY_GUARD_CODES:
            code, reason = _LEGACY_GUARD_CODES[f]
            out.append({"code": code, "field": f, "reason": reason})
    for g in rule_guards:
        out.append({"code": g.get("code") or "policy_rule_weakened", "field": g["field"][:200],
                    "reason": g["reason"][:300]})
    return out


class _SpecGuard:
    """What a guarded spec change becomes: a change request of ``kind`` against ``base_version``."""

    def __init__(self, tenant: str, actor: dict[str, Any], kind: str, base_version: str,
                 payload: dict[str, Any]) -> None:
        self.tenant, self.actor, self.kind, self.base_version, self.payload = tenant, actor, kind, base_version, payload

    def raise_for(self, reasons: list[dict[str, str]], base: dict[str, Any], new: dict[str, Any]) -> Exception:
        from keelson_engine.core.change_requests import GuardedChange, json_diff

        diff = json_diff(base, new)
        target = "policy_set" if self.kind == "policy_set" else "agent_spec"
        return GuardedChange(tenant=self.tenant, actor=self.actor, kind=self.kind, target=target, reasons=reasons,
                             payload=self.payload, diff=diff, base_version=self.base_version,
                             message="raising automatic-approval thresholds, weakening refuse or approval rules, "
                                     "changing the approver role or quorum and re-pointing the planning connector "
                                     "need a second admin")


def _specialist_bindings(spec: dict[str, Any]) -> dict[str, tuple[str, list[str]]]:
    default_conn = str(spec.get("connector") or "merxa")
    out = {}
    for sp in spec.get("specialists") or []:
        out[str(sp.get("slug"))] = (str(sp.get("connector") or default_conn), list(sp.get("tools") or []))
    return out


def _answer_binding(spec: dict[str, Any]) -> tuple[str, list[str]]:
    return (str(spec.get("answer_connector") or spec.get("connector") or "merxa"),
            list(spec.get("answer_tools") or []))


def _bindings(spec: dict[str, Any]) -> dict[str, tuple[str, list[str]]]:
    out = {f"specialist:{k}": v for k, v in _specialist_bindings(spec).items()}
    out["answer"] = _answer_binding(spec)
    return out


async def _validate_semantics(tenant: str, base: dict[str, Any], new: dict[str, Any], *,
                              platform_admin: bool, cosigned: bool = False,
                              guard: _SpecGuard | None = None) -> None:
    from keelson_engine.core import gateway

    settings = get_settings()
    budgets = new.get("budgets") or {}
    if int(budgets.get("max_llm_calls") or 0) > settings.platform_max_llm_calls:
        raise AdminError(422, "budget_exceeds_platform_max",
                         f"max_llm_calls may be at most {settings.platform_max_llm_calls}")
    if int(budgets.get("max_total_tokens") or 0) > settings.platform_max_total_tokens:
        raise AdminError(422, "budget_exceeds_platform_max",
                         f"max_total_tokens may be at most {settings.platform_max_total_tokens}")

    base_policy, new_policy = base.get("policy") or {}, new.get("policy") or {}
    guarded = [f"policy.{f}" for f in GUARDED_POLICY if int(new_policy.get(f) or 0) > int(base_policy.get(f) or 0)]
    if new_policy.get("approver_role") != base_policy.get("approver_role"):
        guarded.append("policy.approver_role")
    if int(new_policy.get("approval_quorum") or 1) < int(base_policy.get("approval_quorum") or 1):
        guarded.append("policy.approval_quorum")
    connector_changed = str(new.get("connector") or "merxa") != str(base.get("connector") or "merxa")
    if connector_changed:
        guarded.append("connector")
    # M5 rules: only reported when no legacy field above already explains the guard (a raised
    # auto_max_lines also widens the default auto rule — one reason is enough)
    rule_guards = _policy_rule_guards(base_policy, new_policy) if not guarded else []
    for g in rule_guards:
        if g["field"] not in guarded:
            guarded.append(g["field"])
    if connector_changed:
        planning = str(new.get("connector"))
        try:
            conn = await reg.get_connector(tenant, planning)
        except reg.ConnectorNotFound as exc:
            raise AdminError(422, "unknown_connector", f"connector {planning!r} does not exist",
                             field="connector") from exc
        if not conn.usable:
            raise AdminError(422, "connector_not_active", f"connector {planning!r} is not active", field="connector")

    base_models, new_models = base.get("models") or {}, new.get("models") or {}
    changed_roles = {r: a for r, a in new_models.items() if base_models.get(r) != a}
    if changed_roles:
        available = await gateway.tenant_models(tenant)
        for role, alias in sorted(changed_roles.items()):
            info = available.get(alias)
            if info is None:
                raise AdminError(422, "unknown_alias", f"model alias {alias!r} is not available to this tenant",
                                 field=f"models.{role}")
            if info["mode"] != "chat":
                raise AdminError(422, "unknown_alias", f"model alias {alias!r} is not a chat model",
                                 field=f"models.{role}")
            if role in TOOL_ROLES and not info["capabilities"]["tool_loop_signatures"]:
                raise AdminError(422, "alias_not_tool_capable",
                                 f"model alias {alias!r} cannot be used by a tool-using role", field=f"models.{role}")

    base_b, new_b = _bindings(base), _bindings(new)
    cache: dict[str, dict[str, Any]] = {}
    for key, (conn_slug, tools) in new_b.items():
        prev_conn, prev_tools = base_b.get(key, ("", []))
        added = list(tools) if conn_slug != prev_conn else [t for t in tools if t not in prev_tools]
        if not added:
            continue
        if conn_slug not in cache:
            try:
                conn = await reg.get_connector(tenant, conn_slug)
            except reg.ConnectorNotFound as exc:
                raise AdminError(422, "unknown_connector", f"connector {conn_slug!r} does not exist",
                                 field=key) from exc
            if not conn.usable:
                raise AdminError(422, "connector_not_active", f"connector {conn_slug!r} is not active", field=key)
            cache[conn_slug] = {t.name: t for t in await reg.list_tools(tenant, conn_slug)}
        tools_by_name = cache[conn_slug]
        for name in added:
            row = tools_by_name.get(name)
            if row is None:
                raise AdminError(422, "unknown_tool", f"tool {name!r} does not exist on {conn_slug!r}", field=key)
            if row.status != "approved" or not row.classification.investigator_bindable:
                raise AdminError(422, "tool_not_bindable",
                                 f"tool {name!r} is not an approved read tool with trusted data", field=key)

    await _validate_quarantine(tenant, base, new)
    await validate_custom_specialists(tenant, base, new)
    # M7 four-eyes: the change is otherwise valid; a guarded part makes it a change request (raised
    # last, so a request is never created for a change that would fail validation on approval)
    if guarded and not (platform_admin or cosigned):
        if guard is None:  # pragma: no cover - every writer passes its guard context
            raise AdminError(403, "guarded_change", "this change requires a second admin", fields=guarded)
        raise guard.raise_for(spec_guard_reasons(guarded, rule_guards), base, new)


def _quarantine_bindings(spec: dict[str, Any]) -> dict[str, tuple[str, list[str]]]:
    """slug → (bound connector, quarantined sources)."""
    default_conn = str(spec.get("connector") or "merxa")
    out = {}
    for sp in spec.get("specialists") or []:
        if isinstance(sp, dict):
            out[str(sp.get("slug"))] = (str(sp.get("connector") or default_conn),
                                        [str(q) for q in (sp.get("quarantined") or [])])
    return out


def _kb_ids(spec: dict[str, Any]) -> list[str] | None:
    ids = (spec.get("investigation") or {}).get("kb_ids")
    return None if ids is None else [str(i) for i in ids]


def _canonical_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


async def _validate_quarantine(tenant: str, base: dict[str, Any], new: dict[str, Any]) -> None:
    """M4 ``SpecialistSpec.quarantined``: a source a specialist adds must point at a real untrusted
    source (module docstring). Unchanged sources are not re-checked (like tool bindings)."""
    base_q, new_q = _quarantine_bindings(base), _quarantine_bindings(new)
    kb_added: list[str] = []
    cache: dict[str, dict[str, Any]] = {}
    for slug, (conn_slug, sources) in new_q.items():
        prev_conn, prev_sources = base_q.get(slug, ("", []))
        added = list(sources) if conn_slug != prev_conn else [q for q in sources if q not in prev_sources]
        field = f"specialists.{slug}.quarantined"
        for source in added:
            if source in QUARANTINE_KB_SOURCES:
                kb_added.append(slug)
                continue
            tool_name = QUARANTINE_SOURCE_TOOLS.get(source)
            if tool_name is None:  # the runtime model rejects unknown names first
                raise AdminError(422, "quarantine_source_invalid", f"unknown quarantine source {source!r}",
                                 field=field)
            if conn_slug not in cache:
                try:
                    conn = await reg.get_connector(tenant, conn_slug)
                except reg.ConnectorNotFound as exc:
                    raise AdminError(422, "unknown_connector", f"connector {conn_slug!r} does not exist",
                                     field=field) from exc
                if not conn.usable:
                    raise AdminError(422, "connector_not_active", f"connector {conn_slug!r} is not active",
                                     field=field)
                cache[conn_slug] = {t.name: t for t in await reg.list_tools(tenant, conn_slug)}
            row = cache[conn_slug].get(tool_name)
            if row is None:
                raise AdminError(422, "quarantine_source_invalid",
                                 f"the {source} source reads the tool {tool_name!r}, which does not exist on "
                                 f"{conn_slug!r}", field=field)
            cls = row.classification
            if (row.status != "approved" or cls.access is not ToolAccess.READ
                    or cls.content_trust is not ContentTrust.UNTRUSTED_TEXT):
                raise AdminError(422, "quarantine_source_invalid",
                                 f"the {source} source needs {tool_name!r} on {conn_slug!r} to be an approved read "
                                 f"tool classified untrusted_text (it is {row.status}, {cls.access.value}, "
                                 f"{cls.content_trust.value})", field=field)

    new_ids = _kb_ids(new)
    if not kb_added and new_ids == _kb_ids(base):
        return
    uses_kb = any(q in QUARANTINE_KB_SOURCES for _conn, qs in new_q.values() for q in qs)
    if new_ids is not None and not new_ids and uses_kb:
        raise AdminError(422, "quarantine_source_invalid",
                         "investigation.kb_ids is empty, so the knowledge_base source would search nothing; "
                         "list at least one knowledge base or clear it (null = all of them)",
                         field="investigation.kb_ids")
    from keelson_engine.core import kb

    existing = {str(k["id"]) for k in await kb.list_kbs(tenant)}
    if new_ids is not None:
        unknown = [i for i in new_ids if _canonical_uuid(i) not in existing]
        if unknown:
            raise AdminError(422, "quarantine_source_invalid",
                             "investigation.kb_ids names knowledge bases that do not exist: "
                             + ", ".join(repr(i[:64]) for i in unknown[:5]), field="investigation.kb_ids")
    elif kb_added and not existing:
        raise AdminError(422, "quarantine_source_invalid",
                         "the knowledge_base source needs an existing knowledge base; create one on the Knowledge "
                         "page first", field=f"specialists.{kb_added[0]}.quarantined")


async def custom_specialist_problems(tenant: str, draft: Any) -> list[str]:
    """Contract 110 §3 checks of one custom specialist against the tenant's current state
    (:func:`keelson_contracts.agents.custom_specialist_violations`): approved read tools on active
    connectors, the model alias is a chat alias of the tenant team that can run a tool loop and is
    not a Pro model, the knowledge bases exist. ``draft`` is a ``CustomSpecialistSpec``."""
    from keelson_contracts.agents import custom_specialist_violations

    from keelson_engine.core import gateway, kb

    catalogue: dict[tuple[str, str], Any] = {}
    active: set[str] = set()
    for conn in await reg.list_connectors(tenant):
        if conn.usable:
            active.add(conn.slug)
        if conn.slug not in {t.connector for t in draft.tools}:
            continue
        for tool in await reg.list_tools(tenant, conn.slug, include_removed=False):
            if tool.status == "approved":
                catalogue[(conn.slug, tool.name)] = tool.classification
    models = await gateway.tenant_models(tenant)
    chat = {a: m for a, m in models.items() if m.get("mode") == "chat"}
    problems = custom_specialist_violations(
        draft, catalogue=catalogue, aliases=set(chat),
        kb_ids={str(k["id"]) for k in await kb.list_kbs(tenant)} if draft.kb_ids else set(),
        active_connectors=active, alias_models={a: str(m.get("provider_model") or "") for a, m in chat.items()},
    )
    info = chat.get(draft.model_alias)
    if info is not None and draft.tools and not (info.get("capabilities") or {}).get("tool_loop_signatures"):
        problems.append(f"model alias {draft.model_alias!r} cannot run a tool-using specialist")
    return problems


async def validate_custom_specialists(tenant: str, base: dict[str, Any], new: dict[str, Any]) -> None:
    """422 ``custom_specialist_invalid`` when an added or edited custom specialist fails
    :func:`custom_specialist_problems` (unchanged entries are not re-checked, like tool bindings)."""
    from keelson_contracts.agents import CustomSpecialistSpec

    before = {str(s.get("slug")): s for s in base.get("custom_specialists") or [] if isinstance(s, dict)}
    for raw in new.get("custom_specialists") or []:
        if not isinstance(raw, dict) or before.get(str(raw.get("slug"))) == raw:
            continue
        field = f"custom_specialists.{raw.get('slug')}"
        try:
            draft = CustomSpecialistSpec.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {"msg": "invalid"}
            raise AdminError(422, "custom_specialist_invalid", str(first.get("msg"))[:300], field=field) from None
        problems = await custom_specialist_problems(tenant, draft)
        if problems:
            raise AdminError(422, "custom_specialist_invalid", problems[0], field=field, problems=problems[:20])


# ───────────────────────────── writes ─────────────────────────────


async def _insert_version(tenant: str, base_version: str, spec: dict[str, Any], actor: dict[str, Any] | str,
                          summary: str) -> dict[str, Any]:
    by = actor.get("username") if isinstance(actor, dict) else str(actor)
    async with tenant_session(tenant) as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"agent_spec|{tenant}"})
        row = await _latest_row(s)
        current = row["version"] if row else DEFAULT_VERSION
        if current != base_version:
            raise AdminError(409, "version_conflict", "the spec changed since you loaded it",
                             latest_version=current)
        res = await s.execute(text("SELECT COALESCE(max(seq), 0) FROM agent_spec"))
        seq = int(res.scalar_one()) + 1
        version = f"v{seq}"
        m = (await s.execute(
            text(
                """
                INSERT INTO agent_spec (tenant, version, seq, spec, created_by, summary, base_version)
                VALUES (:t, :v, :seq, CAST(:spec AS jsonb), :by, :summary, :base)
                RETURNING version, seq, spec, created_by, created_at, summary, base_version
                """
            ),
            {"t": tenant, "v": version, "seq": seq, "spec": dumps(spec), "by": by, "summary": summary,
             "base": base_version},
        )).mappings().one()
    return _envelope(dict(m), tenant, active=True)


async def put_spec(tenant: str, base_version: str, spec_patch: dict[str, Any], actor: dict[str, Any], *,
                   platform_admin: bool, cosigned: bool = False,
                   guard_kind: str = "agent_spec", guard_payload: dict[str, Any] | None = None
                   ) -> tuple[dict[str, Any], bool]:
    """→ ``(envelope, created)``; ``created`` False when the patch changes nothing.

    A guarded change by a tenant-admin raises ``change_requests.GuardedChange`` (kind
    ``guard_kind``, payload ``guard_payload`` or ``{spec_patch}``) unless ``cosigned`` (an approved
    change request re-applying it) or the actor is a platform-admin."""
    if not isinstance(spec_patch, dict) or not spec_patch:
        raise AdminError(422, "spec_invalid", "spec_patch must be a non-empty object")
    if "tenant" in spec_patch or "version" in spec_patch:
        raise AdminError(422, "spec_invalid", "tenant and version are read-only")
    current = await latest(tenant)
    if current["version"] != base_version:
        raise AdminError(409, "version_conflict", "the spec changed since you loaded it",
                         latest_version=current["version"])
    try:
        base = normalize(tenant, current["spec"])
    except AdminError:  # a stored version the current model no longer accepts: compare raw
        base = current["spec"]
    new = normalize(tenant, merge_patch(current["spec"], spec_patch))
    paths = diff_paths(base, new)
    if not paths:
        return current, False
    guard = _SpecGuard(tenant, actor, guard_kind, base_version,
                       guard_payload if guard_payload is not None else {"spec_patch": spec_patch})
    await _validate_semantics(tenant, base, new, platform_admin=platform_admin, cosigned=cosigned, guard=guard)
    created = await _insert_version(tenant, base_version, new, actor, _summary(paths))
    await admin_audit(actor, "agent_spec.update", f"agent_spec:{created['version']}",
                      {"version": base_version, **{p: _get_path(base, p) for p in paths[:20]}},
                      {"version": created["version"], **{p: _get_path(new, p) for p in paths[:20]}}, tenant=tenant)
    return created, True


def _get_path(d: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


async def restore(tenant: str, version: str, actor: dict[str, Any], *, platform_admin: bool,
                  cosigned: bool = False, base_version: str | None = None) -> dict[str, Any]:
    source = await get_version(tenant, version)
    current = await latest(tenant)
    if base_version is not None and current["version"] != base_version:
        raise AdminError(409, "version_conflict", "the spec changed since you loaded it",
                         latest_version=current["version"])
    if source["version"] == current["version"]:
        raise AdminError(409, "already_active", "this version is already the active one")
    base = current["spec"]
    new = normalize(tenant, source["spec"])
    guard = _SpecGuard(tenant, actor, "agent_spec", current["version"], {"restore_version": version})
    await _validate_semantics(tenant, base, new, platform_admin=platform_admin, cosigned=cosigned, guard=guard)
    created = await _insert_version(tenant, current["version"], new, actor, f"restored from {version}")
    await admin_audit(actor, "agent_spec.restore", f"agent_spec:{created['version']}",
                      {"version": current["version"]}, {"version": created["version"], "restored_from": version},
                      tenant=tenant)
    return created


async def create_initial(tenant: str, overrides: dict[str, Any] | None, *, actor: dict[str, Any] | str,
                         summary: str) -> str | None:
    """Spec ``v1`` = built-in default + ``overrides`` (pack ``spec_overrides``), only when the tenant
    has no version yet. Returns the version created, or None."""
    async with tenant_session(tenant) as s:
        if await _latest_row(s) is not None:
            return None
    from keelson_engine.core.policies import strip_unsupported_policy

    spec = normalize(tenant, merge_patch(default_spec_dict(tenant), strip_unsupported_policy(overrides or {})))
    try:
        created = await _insert_version(tenant, DEFAULT_VERSION, spec, actor, summary)
    except AdminError as exc:
        if exc.code == "version_conflict":
            return None
        raise
    await admin_audit(actor, "agent_spec.create", f"agent_spec:{created['version']}", None,
                      {"version": created["version"], "summary": summary}, tenant=tenant)
    return str(created["version"])


async def upgrade_builtins(tenant: str, *, actor: dict[str, Any] | str) -> str | None:
    """Adds the built-in default's specialists that the tenant's spec **history** never contained
    (a release added them, e.g. M4 ``sourcing`` / ``logistics`` / ``playbook``) as a new version,
    and lifts budgets still at a legacy built-in default to the current default. A specialist an
    admin removed later is never re-added (it is in the history). Only specialists whose connector
    the tenant has are added. No semantic validation (like :func:`create_initial`: bootstrap runs
    before the tool snapshot exists; the admin page validates later edits). Returns the version
    created, or None. Idempotent."""
    async with tenant_session(tenant) as s:
        row = await _latest_row(s)
        if row is None:
            return None
        history = [r[0] for r in (await s.execute(text("SELECT spec FROM agent_spec"))).all()]
    seen = {str(sp.get("slug")) for spec in history if isinstance(spec, dict)
            for sp in (spec.get("specialists") or []) if isinstance(sp, dict)}
    current = row["spec"] if isinstance(row["spec"], dict) else {}
    default = default_spec_dict(tenant)
    have = {c.slug for c in await reg.list_connectors(tenant)}
    added = [sp for sp in default.get("specialists") or []
             if str(sp.get("slug")) not in seen and str(sp.get("connector") or default.get("connector")) in have]
    if not added:
        return None
    patch: dict[str, Any] = {"specialists": [*(current.get("specialists") or []), *added]}
    budgets = current.get("budgets") or {}
    if any(all(budgets.get(k) == v for k, v in legacy.items()) for legacy in LEGACY_DEFAULT_BUDGETS):
        patch["budgets"] = default.get("budgets")
    try:
        spec = normalize(tenant, merge_patch(current, patch))
    except AdminError as exc:  # a stored version the current model no longer accepts: leave it to an admin
        log.warning("agent spec upgrade skipped", tenant=tenant, error=exc.message)
        return None
    summary = "built-in specialists added: " + ", ".join(str(sp["slug"]) for sp in added)
    try:
        created = await _insert_version(tenant, str(row["version"]), spec, actor, summary)
    except AdminError as exc:
        if exc.code == "version_conflict":
            return None
        raise
    await admin_audit(actor, "agent_spec.update", f"agent_spec:{created['version']}",
                      {"version": row["version"]}, {"version": created["version"], "summary": summary}, tenant=tenant)
    return str(created["version"])


# ───────────────────────────── cross-module helpers ─────────────────────────────


async def bound_connectors(tenant: str) -> set[str]:
    """Connector slugs referenced by the tenant's latest spec (default spec included)."""
    spec = (await latest(tenant))["spec"]
    out = {conn for conn, _tools in _bindings(spec).values()}
    if spec.get("connector"):
        out.add(str(spec["connector"]))
    return out


async def aliases_in_use() -> dict[str, set[str]]:
    """tenant → model aliases referenced by its latest spec (the built-in default when none)."""
    async with system_session() as s:
        tenants = [str(r[0]) for r in (await s.execute(text("SELECT slug FROM tenant"))).all()]
        res = await s.execute(text(
            "SELECT DISTINCT ON (tenant) tenant, spec FROM agent_spec "
            "ORDER BY tenant, seq DESC NULLS LAST, created_at DESC"))
        specs = {str(r[0]): r[1] for r in res.all()}
    out: dict[str, set[str]] = {}
    for t in tenants:
        spec = specs.get(t) or default_spec_dict(t)
        out[t] = {str(a) for a in (spec.get("models") or {}).values()}
    return out


async def catalog(tenant: str) -> dict[str, Any]:
    from keelson_engine.core import gateway

    current = await latest(tenant)
    try:  # model defaults filled in (e.g. the M4 planner / critic aliases of an older stored version)
        spec = normalize(tenant, current["spec"])
    except AdminError:  # a stored version the current model no longer accepts: show it as stored
        spec = current["spec"]
    try:
        models = await gateway.tenant_models(tenant)
        gateway_ok = True
    except AdminError:
        models, gateway_ok = {}, False
    roles = []
    for role, alias in (spec.get("models") or {}).items():
        roles.append({
            "role": role,
            "display_name": ROLE_DISPLAY.get(role, role.replace("_", " ").capitalize()),
            "alias": alias,
            "prompt_role": ROLE_PROMPT.get(role),
            "prompt": (spec.get("prompts") or {}).get(role) if role in ROLE_PROMPT else None,
            "uses_tools": role in TOOL_ROLES,
        })
    specialists = [
        {"slug": sp.get("slug"),
         "display_name": sp.get("display_name") or SPECIALIST_DISPLAY.get(str(sp.get("slug")), sp.get("slug")),
         "connector": sp.get("connector") or spec.get("connector") or "merxa",
         "tools": list(sp.get("tools") or []),
         "quarantined": list(sp.get("quarantined") or [])}
        for sp in spec.get("specialists") or []
    ]
    aliases = [
        {"alias": a, "mode": info["mode"], "capabilities": info["capabilities"]}
        for a, info in sorted(models.items())
    ]
    connectors_out = []
    for conn in await reg.list_connectors(tenant):
        if conn.status == "disabled":
            continue
        tools = []
        for t in await reg.list_tools(tenant, conn.slug):
            if t.status != "approved":
                continue
            bindable = t.classification.investigator_bindable
            reason = None
            if t.classification.access.value != "read":
                reason = f"{t.classification.access.value} tool (only deterministic steps may call it)"
            elif t.classification.content_trust.value != "trusted_data":
                reason = "returns untrusted free text"
            tools.append({"name": t.name, "title": t.title, "description": t.description,
                          "classification": t.classification.model_dump(mode="json"),
                          "bindable": bindable and conn.usable, "reason": reason if not bindable else
                          (None if conn.usable else f"connector is {conn.status}")})
        connectors_out.append({"slug": conn.slug, "name": conn.name, "status": conn.status, "tools": tools})
    return {
        "version": current["version"],
        "roles": roles,
        "prompt_max_chars": PROMPT_MAX,
        "specialists": specialists,
        "answer": {"connector": _answer_binding(spec)[0], "tools": _answer_binding(spec)[1]},
        "aliases": aliases,
        "gateway_available": gateway_ok,
        "connectors": connectors_out,
        "guarded_fields": list(GUARDED_FIELDS),
        "platform_max": {"max_llm_calls": get_settings().platform_max_llm_calls,
                         "max_total_tokens": get_settings().platform_max_total_tokens},
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    }


async def reset_to_default(tenant: str, overrides: dict[str, Any] | None, *, actor: dict[str, Any] | str,
                           summary: str = "demo reset") -> dict[str, Any]:
    """Make the built-in default + ``overrides`` (a pack's ``spec_overrides``) the active spec
    (M5 demo reset). Version names stay stable (LLM replay keys include the spec version): when a
    stored version already equals the target, it becomes the latest again (its ``seq`` moves to
    the top; nothing is deleted and no name is reused); otherwise a new version is created. Among
    several equal versions the **oldest** wins: an admin edit later restored by saving the old
    values again (a new, equal version) must not move the name the recordings are keyed on.

    Returns ``{"version", "action": "unchanged" | "reactivated" | "created"}``."""
    from keelson_engine.core.policies import strip_unsupported_policy

    target = normalize(tenant, merge_patch(default_spec_dict(tenant), strip_unsupported_policy(overrides or {})))
    async with tenant_session(tenant) as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"agent_spec|{tenant}"})
        rows = [dict(m) for m in (await s.execute(text(
            "SELECT version, seq, spec, created_at FROM agent_spec ORDER BY seq DESC NULLS LAST, created_at DESC"
        ))).mappings().all()]
        match: dict[str, Any] | None = None
        for row in sorted(rows, key=lambda r: (r["created_at"], str(r["version"]))):  # oldest first
            try:
                stored = normalize(tenant, row["spec"]) if isinstance(row["spec"], dict) else None
            except AdminError:
                stored = row["spec"]
            if stored == target:
                match = row
                break
        if match is not None and rows and match["version"] == rows[0]["version"]:
            return {"version": match["version"], "action": "unchanged"}
        if match is not None:
            await s.execute(
                text("UPDATE agent_spec SET seq = (SELECT COALESCE(max(seq), 0) + 1 FROM agent_spec) "
                     "WHERE version = :v"),
                {"v": match["version"]},
            )
            version, action = str(match["version"]), "reactivated"
    if match is None:
        current = rows[0]["version"] if rows else DEFAULT_VERSION
        created = await _insert_version(tenant, str(current), target, actor, summary)
        version, action = str(created["version"]), "created"
    await admin_audit(actor, "agent_spec.reset", f"agent_spec:{version}",
                      {"version": rows[0]["version"] if rows else DEFAULT_VERSION},
                      {"version": version, "action": action, "summary": summary}, tenant=tenant)
    return {"version": version, "action": action}
