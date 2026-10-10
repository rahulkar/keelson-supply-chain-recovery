"""M8 proposed tier-A mappings (``docs/contracts/121-m8-mapping-compiler.md`` §3): the engine
samples a connector's approved, trusted read tools, reduces what it saw to **shape-only source
profiles**, and proposes read / action mapping **drafts** — rule-based (always, no model needed)
and optionally refined by an LLM. A proposal is never activated: an admin opens a draft in the
mapping editor, previews it and saves it like any manual edit (an action mapping that came from a
proposal needs a second admin, change request ``connector_mapping``).

Three groups of shapes:

* **Profiles** (:class:`SourceProfile` / :class:`PathProfile`): per sampled source the paging the
  sampler detected and per item path a *shape* (``code``, ``int``, ``date``, ``text`` …), counts and
  at most three shape-safe examples (codes, integers, dates, weeks, booleans — never free text).
  They are the only thing a model sees of the vendor data.
* **The proposal** (:class:`MappingProposal`): proposed entities / lookups / ops with a confidence
  in basis points, the origin (``heuristic`` / ``llm`` / ``both``) and a rationale per field, notes,
  and :meth:`MappingProposal.read_draft` / :meth:`MappingProposal.action_draft` (complete parts only).
* **The strict LLM output** (:class:`MappingProposalLlm`, an :class:`~keelson_contracts.llm.LlmModel`):
  every field required, maps written as lists, no bounds keywords (strict structured output). The
  model may only reference sampled sources by ``source_ref``; tool arguments and paging always come
  from the deterministic sampler. :func:`mapping_proposal_llm_violations` lists what the engine
  drops before merging.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, ValidationError, model_validator

from .changeset import OP_PAYLOAD_MODELS
from .common import ContractModel, NonNegInt, PositiveInt, Sha256Hex
from .connectors import ContentTrust, ToolAccess, ToolClassification
from .llm import LlmModel
from .mapping import (
    ACTION_TEMPLATE_ROOTS,
    ActionMapping,
    ActionOpMapping,
    EntityMapping,
    Expression,
    ExpressionError,
    FieldExpression,
    LOOKUP_NAME_PATTERN,
    JsonPathLite,
    LookupName,
    LookupTable,
    OpName,
    ReadMapping,
    ReadSource,
    ScnEntityName,
    StaticArgs,
    TemplateError,
    ToolName,
    expression_lookups,
    expression_records,
    is_aggregate,
    parse_expression,
    parse_path,
    template_refs,
)
from .scn import SCN_COLUMNS

__all__ = [
    "CONFIDENCE_BP",
    "ConfidenceBp",
    "LlmArg",
    "LlmLookupEntry",
    "LlmMatch",
    "LlmProposedEntity",
    "LlmProposedField",
    "LlmProposedLookup",
    "LlmProposedOp",
    "MAX_PROFILE_PATHS",
    "MappingProposal",
    "MappingProposalLlm",
    "MappingProposeRequest",
    "PathProfile",
    "PathShape",
    "ProposalNote",
    "ProposalNoteCode",
    "ProposalOrigin",
    "ProposalVersions",
    "ProposeSource",
    "ProposedAction",
    "ProposedAlternative",
    "ProposedEntity",
    "ProposedField",
    "ProposedLookup",
    "ProposedOp",
    "ProposedRead",
    "SOURCE_REF_PATTERN",
    "ShapeCount",
    "SourceProfile",
    "llm_args",
    "mapping_proposal_llm_violations",
]

MAX_PROFILE_PATHS: Final = 200
MAX_EXAMPLES: Final = 3
MAX_RATIONALE: Final = 300
SOURCE_REF_PATTERN: Final = r"^S[1-9][0-9]{0,2}$"
_LOOKUP_NAME_RE: Final = re.compile(LOOKUP_NAME_PATTERN)

ConfidenceBp = Annotated[StrictInt, Field(ge=0, le=10_000)]
"""How sure the proposer is (basis points; shown as a percentage, never acted upon)."""
ProposalOrigin = Literal["heuristic", "llm", "both"]
CONFIDENCE_BP: Final[Mapping[str, int]] = {"high": 8500, "medium": 6000, "low": 3000}
"""The LLM's coarse confidence → basis points (deterministic)."""

PathShape = Literal[
    "code", "int", "int_text", "decimal_text", "date", "datetime", "iso_week", "bool", "float", "text",
    "object", "list", "empty",
]
"""What the sampled values at a path look like (``empty`` = always null)."""
DateFormat = Literal["DD/MM/YYYY", "MM/DD/YYYY", "DD.MM.YYYY", "DD-MM-YYYY", "YYYY/MM/DD"]
SourceRef =Annotated[str, Field(pattern=SOURCE_REF_PATTERN)]
Rationale = Annotated[str, Field(max_length=MAX_RATIONALE)]
Example = Annotated[str, Field(min_length=1, max_length=80)]


# ─────────────────────────────── profiles ───────────────────────────────


class ShapeCount(ContractModel):
    type: Literal["null", "str", "int", "bool", "float", "object", "list"]
    count: NonNegInt


class PathProfile(ContractModel):
    """One item path of a sampled source, reduced to shape statistics."""

    path: JsonPathLite
    shape: PathShape
    date_format: DateFormat | None = None
    """``date`` shape in a non-ISO layout every sampled value parses with (``date($.x, fmt)``)."""
    types: Annotated[list[ShapeCount], Field(max_length=7)]
    null_count: NonNegInt
    distinct_count: NonNegInt
    unique: StrictBool
    """Every non-null sampled value differs (a key candidate)."""
    max_len: NonNegInt
    """Longest text value (characters)."""
    examples: Annotated[list[Example], Field(max_length=MAX_EXAMPLES)]
    """Shape-safe values only (codes, integers, dates, weeks, booleans); free text is withheld."""


class SourceProfile(ContractModel):
    """A sampled paged read: ``source`` is the paging the sampler detected (a ready
    :class:`~keelson_contracts.mapping.ReadSource` for a draft), ``ref`` how proposals name it."""

    ref: SourceRef
    source: ReadSource
    variant: Annotated[str, Field(max_length=64)] | None
    """The discovered argument value that selects the record type (``sku``, ``store`` …)."""
    items_sampled: NonNegInt
    total_hint: NonNegInt | None
    paths: Annotated[list[PathProfile], Field(max_length=MAX_PROFILE_PATHS)]


# ─────────────────────────────── the proposal ───────────────────────────────


class ProposedAlternative(ContractModel):
    expression: FieldExpression
    origin: ProposalOrigin
    confidence_bp: ConfidenceBp


class ProposedField(ContractModel):
    column: Annotated[str, Field(min_length=1, max_length=64)]
    expression: FieldExpression
    confidence_bp: ConfidenceBp
    origin: ProposalOrigin
    rationale: Rationale
    alternatives: Annotated[list[ProposedAlternative], Field(max_length=3)] = Field(default_factory=list)


class ProposedEntity(ContractModel):
    """An entity mapping draft. ``missing_required`` lists required columns nobody could map:
    such an entity is shown but left out of :meth:`MappingProposal.read_draft`."""

    entity: ScnEntityName
    source_ref: SourceRef
    source: ReadSource
    group_by: Annotated[list[Expression], Field(min_length=1, max_length=4)] | None = None
    dedupe: StrictBool = False
    fields: list[ProposedField]
    missing_required: list[str] = Field(default_factory=list)
    confidence_bp: ConfidenceBp
    origin: ProposalOrigin
    rationale: Rationale

    @model_validator(mode="after")
    def _columns(self) -> ProposedEntity:
        known = set(SCN_COLUMNS[self.entity])
        names = [f.column for f in self.fields]
        if unknown := sorted(set(names) - known):
            raise ValueError(f"{self.entity} has no columns {unknown}")
        if len(set(names)) != len(names):
            raise ValueError("each column is proposed once")
        if unknown := sorted(set(self.missing_required) - known):
            raise ValueError(f"{self.entity} has no columns {unknown}")
        return self

    def mapping(self) -> EntityMapping | None:
        """The entity mapping (``None`` while required columns are missing or it does not validate)."""
        if self.missing_required:
            return None
        try:
            return EntityMapping.model_validate({
                "entity": self.entity, "source": self.source.model_dump(mode="json"),
                "fields": {f.column: f.expression for f in self.fields}, "dedupe": self.dedupe,
                **({"group_by": list(self.group_by)} if self.group_by else {}),
            })
        except ValidationError:
            return None


class ProposedLookup(ContractModel):
    name: LookupName
    table: LookupTable
    source_ref: SourceRef | None
    confidence_bp: ConfidenceBp
    origin: ProposalOrigin
    rationale: Rationale


class ProposedOp(ContractModel):
    mapping: ActionOpMapping
    confidence_bp: ConfidenceBp
    origin: ProposalOrigin
    rationale: Rationale
    generic: StrictBool = False
    """Mapped onto a generic update tool (``update_item(type, key, fields_json, …)``): check the
    vendor's business rules before relying on it."""


ProposalNoteCode = Literal[
    "llm_disabled", "llm_unavailable", "llm_failed", "replay_miss", "llm_output_dropped", "injection_suspected",
    "description_flagged", "sample_value_withheld", "sampling_truncated", "schema_only", "required_column_unmapped",
    "entity_unmapped", "op_unmapped", "path_not_in_sample", "type_mismatch", "generic_update_tool", "source_unused",
    "key_dropped", "sample_failed",
]


class ProposalNote(ContractModel):
    code: ProposalNoteCode
    severity: Literal["info", "warning"]
    where: Annotated[str, Field(max_length=200)] | None
    message: Annotated[str, Field(min_length=1, max_length=MAX_RATIONALE)]


class ProposeSource(ContractModel):
    """An admin-named read to sample in addition to the discovered ones."""

    tool: ToolName
    args: StaticArgs = Field(default_factory=dict)


class MappingProposeRequest(ContractModel):
    """``POST /api/admin/connectors/{slug}/mappings/proposals``. ``proposer: heuristic`` never
    calls a model; ``auto`` refines the rule-based draft with the LLM when one is available
    (otherwise the rule-based draft comes back with a note). ``sample: false`` proposes from the
    tool catalogue alone (no vendor call; also for a connector that is not active)."""

    kinds: Annotated[list[Literal["read", "action"]], Field(min_length=1, max_length=2)]
    proposer: Literal["auto", "heuristic"] = "auto"
    sample: StrictBool = True
    entities: Annotated[list[ScnEntityName], Field(min_length=1, max_length=15)] | None = None
    """Only these entities (default: every entity a source fits)."""
    ops: Annotated[list[OpName], Field(min_length=1, max_length=32)] | None = None
    sources: Annotated[list[ProposeSource], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> MappingProposeRequest:
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("kinds must be unique")
        return self


class ProposedRead(ContractModel):
    entities: list[ProposedEntity]
    lookups: list[ProposedLookup] = Field(default_factory=list)


class ProposedAction(ContractModel):
    ops: list[ProposedOp]


class ProposalVersions(ContractModel):
    read: PositiveInt | None = None
    action: PositiveInt | None = None


class MappingProposal(ContractModel):
    """A stored proposal (``connector_mapping_proposal``): drafts for review, never active."""

    id: Annotated[str, Field(min_length=1, max_length=64)]
    connector: Annotated[str, Field(min_length=1, max_length=63)]
    status: Literal["ready", "accepted", "discarded"]
    proposer: Literal["heuristic", "llm+heuristic"]
    model_alias: Annotated[str, Field(max_length=100)] | None
    created_by: Annotated[str, Field(max_length=200)] | None
    created_at: AwareDatetime
    inputs_hash: Sha256Hex
    """sha256 of the sanitised profiles, the approved tool definitions and the request."""
    base_versions: ProposalVersions
    """The mapping versions current when the proposal was made."""
    profiles: list[SourceProfile]
    read: ProposedRead | None
    action: ProposedAction | None
    unmapped_entities: list[ScnEntityName] = Field(default_factory=list)
    unmapped_ops: list[OpName] = Field(default_factory=list)
    notes: list[ProposalNote] = Field(default_factory=list)
    accepted: ProposalVersions | None = None
    """The versions saved from this proposal (``status: accepted``)."""
    llm_calls: NonNegInt = 0

    def read_draft(self) -> ReadMapping | None:
        """The complete proposed entities (+ the lookups they use) as a read mapping, or ``None``."""
        if self.read is None:
            return None
        entities = [m for m in (e.mapping() for e in self.read.entities) if m is not None]
        if not entities:
            return None
        used: set[str] = set()
        for em in entities:
            for ast in [*em.parsed_fields().values(), *em.parsed_group_by()]:
                used |= expression_lookups(ast) | expression_records(ast)
        lookups = {lk.name: lk.table for lk in self.read.lookups if lk.name in used}
        try:
            return ReadMapping(entities=entities, lookups=lookups)
        except ValidationError:
            return None

    def action_draft(self) -> ActionMapping | None:
        """The proposed ops as an action mapping, or ``None``."""
        if self.action is None or not self.action.ops:
            return None
        return ActionMapping(ops=[o.mapping for o in self.action.ops])


# ─────────────────────────────── strict LLM output ───────────────────────────────

LlmConfidence = Literal["high", "medium", "low"]


class LlmProposedField(LlmModel):
    column: str
    expression: str
    confidence: LlmConfidence
    rationale: str


class LlmProposedEntity(LlmModel):
    entity: ScnEntityName
    source_ref: str
    fields: list[LlmProposedField]
    dedupe: bool
    group_by: list[str]
    """Empty = not grouped."""
    rationale: str


class LlmLookupEntry(LlmModel):
    key: str
    value: str


class LlmProposedLookup(LlmModel):
    name: str
    kind: Literal["static", "source", "record"]
    source_ref: str | None
    key: str | None
    value: str | None
    values: list[LlmLookupEntry]
    """``static`` tables only."""


class LlmArg(LlmModel):
    """One argument: ``template`` = a text template (``"{{payload.qty}}"``), ``json_template`` = a
    JSON template text sent as canonical JSON (``{"$json": …}``), or a literal."""

    name: str
    kind: Literal["template", "json_template", "integer", "boolean", "null"]
    text: str | None
    integer: int | None
    boolean: bool | None


class LlmMatch(LlmModel):
    path: str
    template: str


class LlmProposedOp(LlmModel):
    op: str
    tool: str
    args: list[LlmArg]
    request_id_arg: str | None
    readback_tool: str | None
    readback_args: list[LlmArg]
    readback_items_path: str | None
    readback_match: list[LlmMatch]
    confidence: LlmConfidence
    rationale: str


class MappingProposalLlm(LlmModel):
    """Strict output of the ``mapping_proposer`` agent (tool-free). ``injection_suspected``: the
    model saw instructions inside the data — the whole output is discarded."""

    entities: list[LlmProposedEntity]
    lookups: list[LlmProposedLookup]
    ops: list[LlmProposedOp]
    injection_suspected: bool


def llm_args(args: Sequence[LlmArg]) -> dict[str, Any]:
    """LLM argument list → an argument template (raises ``ValueError``)."""
    out: dict[str, Any] = {}
    for a in args:
        if a.name in out:
            raise ValueError(f"argument {a.name!r} given twice")
        if a.kind == "template":
            if a.text is None:
                raise ValueError(f"argument {a.name!r}: a template needs text")
            out[a.name] = a.text
        elif a.kind == "json_template":
            if a.text is None:
                raise ValueError(f"argument {a.name!r}: a JSON template needs text")
            try:
                parsed = json.loads(a.text, parse_float=_no_float)
            except ValueError as exc:
                raise ValueError(f"argument {a.name!r}: not a JSON template ({exc})") from None
            out[a.name] = {"$json": parsed}
        elif a.kind == "integer":
            if a.integer is None:
                raise ValueError(f"argument {a.name!r}: an integer literal needs integer")
            out[a.name] = a.integer
        elif a.kind == "boolean":
            if a.boolean is None:
                raise ValueError(f"argument {a.name!r}: a boolean literal needs boolean")
            out[a.name] = a.boolean
        else:
            out[a.name] = None
    template_refs(out, roots=ACTION_TEMPLATE_ROOTS)
    return out


def _no_float(text: str) -> Any:
    raise ValueError(f"floats are not allowed ({text})")


def _expr_problem(text: str, *, aggregates: bool = False) -> str | None:
    try:
        parse_expression(text, aggregates=aggregates)
    except ExpressionError as exc:
        return str(exc)
    return None


def _tool_problem(name: str | None, tools: Mapping[str, ToolClassification], *, read: bool) -> str | None:
    if name is None:
        return None
    cls = tools.get(name)
    if cls is None:
        return f"tool {name!r} is not an approved tool of this connector"
    if read and (cls.access is not ToolAccess.READ or cls.content_trust is ContentTrust.UNTRUSTED_TEXT):
        return f"tool {name!r} is not an approved trusted read tool"
    if not read and cls.access is ToolAccess.READ:
        return f"tool {name!r} is a read tool"
    return None


def mapping_proposal_llm_violations(
    out: MappingProposalLlm,
    *,
    profiles: Sequence[SourceProfile],
    tools: Mapping[str, ToolClassification],
) -> list[tuple[str, str]]:
    """``(where, problem)`` for every LLM-proposed item the engine must drop: unknown source refs,
    unknown columns, expressions outside the grammar, malformed lookups, unknown ops, tools that
    are not approved (or have the wrong access), argument templates that do not validate. ``where``
    is ``entities[i]`` / ``entities[i].fields.<column>`` / ``lookups[i]`` / ``ops[i]``."""
    problems: list[tuple[str, str]] = []
    refs = {p.ref for p in profiles}
    for i, e in enumerate(out.entities):
        where = f"entities[{i}]"
        if e.source_ref not in refs:
            problems.append((where, f"unknown source_ref {e.source_ref[:20]!r}"))
            continue
        for j, g in enumerate(e.group_by):
            if p := _expr_problem(g):
                problems.append((where, f"group_by[{j}]: {p}"))
        if e.group_by and e.dedupe:
            problems.append((where, "group_by and dedupe exclude each other"))
        known = set(SCN_COLUMNS[e.entity])
        seen: set[str] = set()
        for f in e.fields:
            at = f"{where}.fields.{f.column[:64]}"
            if f.column not in known:
                problems.append((at, f"{e.entity} has no column {f.column[:64]!r}"))
            elif f.column in seen:
                problems.append((at, "column proposed twice"))
            elif p := _expr_problem(f.expression, aggregates=bool(e.group_by)):
                problems.append((at, p))
            seen.add(f.column)
    for i, lk in enumerate(out.lookups):
        where = f"lookups[{i}]"
        problem = _lookup_problem(lk, refs)
        if problem:
            problems.append((where, problem))
    for i, op in enumerate(out.ops):
        where = f"ops[{i}]"
        if op.op not in OP_PAYLOAD_MODELS:
            problems.append((where, f"unknown op {op.op[:64]!r}"))
            continue
        tool_problems = [p for p in (_tool_problem(op.tool, tools, read=False),
                                     _tool_problem(op.readback_tool, tools, read=True)) if p]
        problems += [(where, p) for p in tool_problems]
        try:
            args = llm_args(op.args)
            readback_args = llm_args(op.readback_args)
            for m in op.readback_match:
                parse_path(m.path)
                template_refs(m.template, roots=ACTION_TEMPLATE_ROOTS)
            if op.readback_items_path is not None:
                parse_path(op.readback_items_path)
        except (ValueError, TemplateError, ExpressionError) as exc:
            problems.append((where, str(exc)[:200]))
            continue
        if tool_problems:
            continue
        # the op as the engine would store it (idempotency = the confirmed classification)
        idem = tools[op.tool].idempotency.value
        if idem == "enforced" and op.request_id_arg is None:
            idem = "best_effort"
        readback = None if op.readback_tool is None else {
            "tool": op.readback_tool, "args": readback_args, "items_path": op.readback_items_path,
            "match": {m.path: m.template for m in op.readback_match}}
        try:
            ActionOpMapping.model_validate({"op": op.op, "tool": op.tool, "args": args, "idempotency": idem,
                                            "request_id_arg": op.request_id_arg, "readback": readback})
        except ValidationError as exc:
            problems.append((where, str(exc.errors()[0].get("msg", ""))[:200]))
    return problems


def _lookup_problem(lk: LlmProposedLookup, refs: set[str]) -> str | None:
    if not _LOOKUP_NAME_RE.fullmatch(lk.name):
        return f"lookup name {lk.name[:40]!r} does not match {LOOKUP_NAME_PATTERN}"
    if lk.kind == "static":
        if lk.source_ref is not None or not lk.values:
            return "a static lookup has values and no source_ref"
        return None
    if lk.source_ref not in refs:
        return f"unknown source_ref {str(lk.source_ref)[:20]!r}"
    if lk.key is None or (p := _expr_problem(lk.key)):
        return f"key: {p if lk.key is not None else 'missing'}"
    if lk.kind == "source" and (lk.value is None or (p := _expr_problem(lk.value))):
        return f"value: {p if lk.value is not None else 'missing'}"
    # a record table holds whole items: a value the model sent anyway is ignored (never parsed)
    asts = [parse_expression(t) for t in (lk.key, lk.value if lk.kind == "source" else None) if t is not None]
    if any(expression_lookups(a) or expression_records(a) or is_aggregate(a) for a in asts):
        return "a lookup's key/value may not call lookup() or field()"
    return None
