"""Run request envelope (engine-api → engine-worker) and SSE case cursor.

``RunRequest {run_id, tenant, case_id, kind, actor, payload}`` where ``payload`` is
typed by ``kind`` (``50-engine-workflow.md``):

* ``chat``   → ``{text}``
* ``action`` → ``{type: apply|guide|promote|confirm_assumptions, option_id, plan_version, assumption_keys?}``,
  M5 ``{type: rollback, change_set_id}`` and ``{type: checklist_step, checklist_id, seq, status: done|skipped}``
* ``signal`` → ``{signal_id}`` (M5 re-plan) or ``{event_id, alert_id}`` (M6 Signal Hub event)
* ``resume`` → ``{interrupt_id, decision, …}`` (server-built from an interrupt decision)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import Field, SerializerFunctionWrapHandler, StrictInt, model_serializer, model_validator

from .common import ContractModel, NonNegInt, PositiveInt, Sha256Hex, TenantSlug
from .signals import UUID_PATTERN

__all__ = [
    "APPROACH_DECISIONS",
    "ACTION_TYPES",
    "CHECKLIST_STEP_STATUSES",
    "ChecklistStepStatus",
    "OPTION_ACTION_TYPES",
    "APPROVAL_DECISIONS",
    "ActionType",
    "ApproachEdit",
    "ActionPayload",
    "Actor",
    "CaseCursor",
    "ChatPayload",
    "Decider",
    "Decision",
    "MAX_DECISION_REASON_CHARS",
    "MAX_EDIT_IDS",
    "MAX_EVENT_SEQ",
    "MAX_PAYLOAD_KEYS",
    "MAX_SIGNAL_REF_CHARS",
    "PAYLOAD_BY_KIND",
    "RUNS_WAKE_STREAM",
    "ResumePayload",
    "RunKind",
    "RunPayload",
    "RunRequest",
    "SignalPayload",
    "adk_app_name",
    "case_stream_key",
]

RunKind = Literal["chat", "action", "signal", "resume"]
Decision = Literal["confirm", "edit", "what_if", "cancel", "approve", "reject", "revise"]

APPROACH_DECISIONS: Final = frozenset({"confirm", "edit", "what_if", "cancel"})
APPROVAL_DECISIONS: Final = frozenset({"approve", "reject", "revise"})

RUNS_WAKE_STREAM: Final = "keelson:runs:wake"
"""Valkey stream the API ``XADD``s to after inserting a queued run."""

MAX_CHAT_CHARS: Final = 20_000

MAX_EVENT_SEQ: Final = 2**63 - 1
"""``run_event.id`` is a Postgres ``bigint``; larger cursors are rejected, not sent to SQL."""


def case_stream_key(case_id: str) -> str:
    """Valkey stream that live events for a case are tailed from."""
    return f"keelson:case:{case_id}"


def adk_app_name(tenant: str) -> str:
    """ADK ``App.name`` for a tenant: ``"t_<tenant>"`` with ``-`` → ``_``."""
    return "t_" + tenant.replace("-", "_")


class Actor(ContractModel):
    """The authenticated user on whose behalf a run executes."""

    sub: Annotated[str, Field(min_length=1)]
    username: Annotated[str, Field(min_length=1)]
    name: str
    roles: list[str]

    def has_role(self, role: str) -> bool:
        return role in self.roles


class ChatPayload(ContractModel):
    text: Annotated[str, Field(min_length=1, max_length=MAX_CHAT_CHARS)]


ActionType = Literal["apply", "guide", "promote", "confirm_assumptions", "rollback", "checklist_step"]
ACTION_TYPES: Final[tuple[str, ...]] = get_args(ActionType)
OPTION_ACTION_TYPES: Final = frozenset({"apply", "guide", "promote", "confirm_assumptions"})
"""Action types that act on a plan option (they carry ``option_id`` + ``plan_version``)."""
ChecklistStepStatus = Literal["done", "skipped"]
CHECKLIST_STEP_STATUSES: Final[tuple[str, ...]] = get_args(ChecklistStepStatus)
"""What a person may set on a guide step by hand (``verified`` is set only by read-back)."""

NonEmptyStr = Annotated[str, Field(min_length=1)]
EditKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]

MAX_PAYLOAD_KEYS: Final = 50
"""Most assumption keys / typed edits / removed checks one client decision may carry."""
MAX_EDIT_IDS: Final = 500
"""Most ids one typed ``ids`` edit may carry."""
MAX_DECISION_REASON_CHARS: Final = 500
"""Longest note a person may attach to a reject/revise decision (M5)."""


def _is_empty(value: Any) -> bool:
    return not value


def _is_none(value: Any) -> bool:
    return value is None


def _m5_optional() -> Any:
    """An M5 action field: ``None`` by default and omitted from dumps while unset."""
    return Field(default=None, exclude_if=_is_none)


class ActionPayload(ContractModel):
    """A case action (``POST /api/cases/{id}/actions``).

    * option actions (:data:`OPTION_ACTION_TYPES`: ``apply``, ``guide``, ``promote``,
      ``confirm_assumptions``) carry ``option_id`` + ``plan_version``;
      ``confirm_assumptions`` (M4, ``90`` §5) also carries the ``assumption_keys`` the user
      confirms (re-runs impact + simulate only); the other types must not carry keys;
    * ``rollback`` (M5, ``95`` §3) carries only ``change_set_id`` (the applied change set to undo);
    * ``checklist_step`` (M5, ``95`` §2) carries ``checklist_id``, the step ``seq`` and the
      manual ``status`` (``done`` | ``skipped``).

    Fields a type does not use must be absent. Every optional field is omitted from dumps
    while unset, so pre-M5 payloads serialize unchanged.
    """

    type: ActionType
    option_id: NonEmptyStr | None = _m5_optional()
    plan_version: PositiveInt | None = _m5_optional()
    assumption_keys: Annotated[list[NonEmptyStr], Field(max_length=MAX_PAYLOAD_KEYS)] = Field(
        default=[], exclude_if=_is_empty
    )
    change_set_id: Annotated[str, Field(min_length=1, max_length=200)] | None = _m5_optional()
    checklist_id: Annotated[str, Field(min_length=1, max_length=200)] | None = _m5_optional()
    seq: PositiveInt | None = _m5_optional()
    status: ChecklistStepStatus | None = _m5_optional()

    @model_validator(mode="after")
    def _fields_for_type(self) -> ActionPayload:
        own = {
            "rollback": {"change_set_id"},
            "checklist_step": {"checklist_id", "seq", "status"},
        }.get(self.type, {"option_id", "plan_version"})
        present = {
            name for name in ("option_id", "plan_version", "change_set_id", "checklist_id", "seq", "status")
            if getattr(self, name) is not None
        }
        if missing := sorted(own - present):
            raise ValueError(f"{self.type} requires {', '.join(missing)}")
        if extra := sorted(present - own):
            raise ValueError(f"{', '.join(extra)} not allowed for {self.type}")
        if self.type == "confirm_assumptions":
            if not self.assumption_keys:
                raise ValueError("confirm_assumptions requires assumption_keys")
            if len(set(self.assumption_keys)) != len(self.assumption_keys):
                raise ValueError("assumption_keys must be unique")
        elif self.assumption_keys:
            raise ValueError(f"assumption_keys are only allowed for confirm_assumptions (got {self.type!r})")
        return self


class ApproachEdit(ContractModel):
    """A typed **Edit** of an approach-card value (``key`` = an ``editable`` key).

    Exactly one of ``value_int`` (days / percent) or ``value_ids`` (scope ids) is set.
    """

    key: EditKey
    value_int: StrictInt | None = None
    value_ids: Annotated[list[NonEmptyStr], Field(min_length=1, max_length=MAX_EDIT_IDS)] | None = None

    @model_validator(mode="after")
    def _one_value(self) -> ApproachEdit:
        if (self.value_int is None) == (self.value_ids is None):
            raise ValueError("an edit sets exactly one of value_int or value_ids")
        if self.value_ids is not None and len(set(self.value_ids)) != len(self.value_ids):
            raise ValueError("value_ids must be unique")
        return self


MAX_SIGNAL_REF_CHARS: Final = 200


class SignalPayload(ContractModel):
    """A ``signal`` run's payload, one of two shapes:

    * M5 re-plan: ``{signal_id}`` — a ``case_signal`` row written by the webhook re-plan router;
    * M6 external signal (``100`` §6): ``{event_id, alert_id}`` — a Signal Hub event (``alert_id``
      is the alert the case was opened from, ``null`` when opened without one).

    Exactly one of ``signal_id`` / ``event_id`` is set; ``alert_id`` only with ``event_id``;
    ``event_id`` / ``alert_id`` are canonical lowercase UUIDs (Signal Hub ids). Dumps
    keep each shape exact: ``{signal_id}`` or ``{event_id, alert_id}`` (``alert_id`` may be null).
    """

    signal_id: Annotated[str, Field(min_length=1, max_length=MAX_SIGNAL_REF_CHARS)] | None = None
    event_id: Annotated[str, Field(pattern=UUID_PATTERN)] | None = None
    """A Signal Hub event id: a canonical lowercase UUID (``signals.event_id``). Constrained because
    the engine puts it into Signal Hub URL paths (``/v1/events/{id}``): no ``/``, ``..``, ``?``."""
    alert_id: Annotated[str, Field(pattern=UUID_PATTERN)] | None = None
    """The Signal Hub alert id (canonical lowercase UUID), or ``None``."""

    @model_validator(mode="after")
    def _one_shape(self) -> SignalPayload:
        if (self.signal_id is None) == (self.event_id is None):
            raise ValueError("a signal payload carries exactly one of signal_id or event_id")
        if self.alert_id is not None and self.event_id is None:
            raise ValueError("alert_id is only allowed with event_id")
        return self

    @model_serializer(mode="wrap")
    def _exact_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        keep = ("signal_id",) if self.signal_id is not None else ("event_id", "alert_id")
        return {k: v for k, v in data.items() if k in keep}

    @property
    def is_external(self) -> bool:
        """True for an M6 Signal Hub event (``{event_id, alert_id}``)."""
        return self.event_id is not None


class Decider(ContractModel):
    """One person whose decision resolved an interrupt (``decided_by`` entry).

    Carries what the engine needs to mint the approval receipt (``keelson_sub``,
    ``approver_name``) and to fetch the approver's delegated Merxa token.
    """

    sub: Annotated[str, Field(min_length=1)]
    username: Annotated[str, Field(min_length=1)]
    name: str
    role: str | None = None
    """Role that satisfied the quorum (e.g. ``approver``); ``None`` for approach decisions."""


class ResumePayload(ContractModel):
    """Server-built resume after an interrupt decision (never taken verbatim from a client).

    ``core.interrupts.record_decision`` inserts it as
    ``{interrupt_id, decision, decided_by: [...]}`` (``55-engine-internals.md``) plus the
    decision-specific fields. Interrupt ids built per ``50-engine-workflow.md``
    (``approach-…`` / ``approval-…``) only accept decisions of their own kind.

    ``edit`` carries either free text (M1) or, from M4, typed edits
    (``[{key, value_int | value_ids}]``, see :class:`ApproachEdit`) and/or
    ``removed_checks`` (plan task ids the user removed). Typed edits and removed checks
    are only allowed with ``edit``; ``removed_checks`` is omitted from dumps while empty.
    """

    interrupt_id: Annotated[str, Field(min_length=1)]
    decision: Decision
    plan_version: PositiveInt | None = None
    change_set_hash: Sha256Hex | None = None
    edits: str | Annotated[list[ApproachEdit], Field(max_length=MAX_PAYLOAD_KEYS)] | None = None
    decided_by: list[Decider] = Field(default_factory=list)
    removed_checks: Annotated[list[NonEmptyStr], Field(max_length=MAX_PAYLOAD_KEYS)] = Field(
        default=[], exclude_if=_is_empty
    )
    reason: Annotated[str, Field(min_length=1, max_length=MAX_DECISION_REASON_CHARS)] | None = _m5_optional()
    """M5: the decider's note on a ``reject``/``revise`` (shown on the approval card)."""

    @model_validator(mode="after")
    def _decision_requirements(self) -> ResumePayload:
        if self.reason is not None and self.decision not in ("reject", "revise"):
            raise ValueError("reason is only allowed with decision 'reject' or 'revise'")
        if self.decision == "approve" and (self.plan_version is None or self.change_set_hash is None):
            raise ValueError("approve requires plan_version and change_set_hash")
        if self.decision == "edit" and not self.edits and not self.removed_checks:
            raise ValueError("edit requires edits text, typed edits or removed_checks")
        if self.decision != "edit" and (isinstance(self.edits, list) or self.removed_checks):
            raise ValueError("typed edits and removed_checks are only allowed with decision 'edit'")
        if isinstance(self.edits, list) and len({e.key for e in self.edits}) != len(self.edits):
            raise ValueError("typed edits must not repeat a key")
        if len(set(self.removed_checks)) != len(self.removed_checks):
            raise ValueError("removed_checks must be unique")
        if self.interrupt_id.startswith("approach-") and self.decision not in APPROACH_DECISIONS:
            raise ValueError(f"decision {self.decision!r} is not valid for an approach interrupt")
        if self.interrupt_id.startswith("approval-") and self.decision not in APPROVAL_DECISIONS:
            raise ValueError(f"decision {self.decision!r} is not valid for an approval interrupt")
        if len({d.sub for d in self.decided_by}) != len(self.decided_by):
            raise ValueError("decided_by must not list the same sub twice")
        return self

    @property
    def interrupt_kind(self) -> Literal["approach", "approval"]:
        return "approach" if self.decision in APPROACH_DECISIONS else "approval"


RunPayload = ChatPayload | ActionPayload | SignalPayload | ResumePayload

PAYLOAD_BY_KIND: Final[Mapping[str, type[ContractModel]]] = {
    "chat": ChatPayload,
    "action": ActionPayload,
    "signal": SignalPayload,
    "resume": ResumePayload,
}


class RunRequest(ContractModel):
    """Typed run envelope stored on the ``run`` row and consumed by the worker."""

    run_id: Annotated[str, Field(min_length=1)]
    tenant: TenantSlug
    case_id: Annotated[str, Field(min_length=1)]
    kind: RunKind
    actor: Actor
    payload: RunPayload

    @model_validator(mode="before")
    @classmethod
    def _payload_by_kind(cls, data: Any) -> Any:
        # Validate the payload against the model selected by ``kind`` so error
        # messages are precise and payloads can never be ambiguous.
        if isinstance(data, Mapping):
            model = PAYLOAD_BY_KIND.get(str(data.get("kind")))
            payload = data.get("payload")
            if model is not None and isinstance(payload, Mapping):
                data = {**data, "payload": model.model_validate(payload)}
        return data

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> RunRequest:
        expected = PAYLOAD_BY_KIND[self.kind]
        if not isinstance(self.payload, expected):
            raise ValueError(f"kind={self.kind!r} requires payload {expected.__name__}")
        return self

    @property
    def app_name(self) -> str:
        return adk_app_name(self.tenant)


class CaseCursor(ContractModel):
    """Where an SSE subscriber resumes: replay events with ``seq > after_seq``."""

    tenant: TenantSlug
    case_id: Annotated[str, Field(min_length=1)]
    after_seq: Annotated[NonNegInt, Field(le=MAX_EVENT_SEQ)] = 0

    @classmethod
    def from_request(
        cls,
        *,
        tenant: str,
        case_id: str,
        last_event_id: str | None = None,
        after: str | int | None = None,
    ) -> CaseCursor:
        """Build from the ``Last-Event-ID`` header (preferred) or the ``?after=`` query value.

        Raises ``ValueError`` on a malformed cursor (the API should answer 400).
        """
        raw: str | int | None = last_event_id if last_event_id not in (None, "") else after
        seq = 0
        if raw not in (None, ""):
            text = str(raw).strip()
            if not (text.isascii() and text.isdigit()) or len(text) > 19:
                raise ValueError(f"invalid event cursor: {raw!r}")
            seq = int(text)
            if seq > MAX_EVENT_SEQ:
                raise ValueError(f"invalid event cursor: {raw!r}")
        return cls(tenant=tenant, case_id=case_id, after_seq=seq)

    @property
    def stream_key(self) -> str:
        return case_stream_key(self.case_id)
