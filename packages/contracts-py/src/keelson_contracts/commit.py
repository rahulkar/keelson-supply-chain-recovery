"""Per-op commit ledger for tier-A connectors (``110-m7-backoffice-generic.md`` §4, plan §10
"Commit service").

A tier-A connector has no change-set tools: the commit service applies each mapped line with
one tool call (:class:`~keelson_contracts.mapping.ActionOpMapping`) and records it in
``commit_ledger(tenant, change_set_id, line_seq, state, …)``. States::

    attempted ─┬─> succeeded ──> verified        (read-back confirmed the effect)
               ├─> failed                         (the tool refused: definitive, nothing changed)
               └─> in_doubt                       (timeout / transport error: unknown outcome)

The retry rules are data-driven by the tool's confirmed idempotency and are the same for every
connector (:func:`ledger_next_action`):

* ``enforced`` lines are retried with the **same** request id (the target system replays);
* ``best_effort`` lines are read back before any retry (a found effect → succeeded, else retry);
* ``none`` lines are never auto-retried: read back, and if the effect is not found the line
  becomes a guided step.

Atomic groups on a tier-A connector are refused for bulk apply (the group is guided) unless
every member op is ``enforced`` (:func:`atomic_group_bulk_allowed`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, model_validator

from .common import ContractModel, PositiveInt
from .connectors import Idempotency

__all__ = [
    "COMMIT_LEDGER_STATES",
    "CommitLedgerEntry",
    "CommitLedgerState",
    "LedgerAction",
    "LedgerReadback",
    "MAX_AUTO_ATTEMPTS",
    "TERMINAL_LEDGER_STATES",
    "atomic_group_bulk_allowed",
    "ledger_next_action",
]

CommitLedgerState = Literal["attempted", "succeeded", "in_doubt", "failed", "verified"]
"""``verified`` (already in the M1 ``commit_ledger`` check constraint) = read-back confirmed."""
COMMIT_LEDGER_STATES: Final[tuple[str, ...]] = ("attempted", "succeeded", "in_doubt", "failed", "verified")
TERMINAL_LEDGER_STATES: Final = frozenset({"succeeded", "failed", "verified"})
MAX_AUTO_ATTEMPTS: Final = 3
"""Upper bound of automatic attempts of one line (first try included)."""

LedgerAction = Literal["none", "retry", "readback", "guide"]

OpName = Annotated[str, Field(max_length=64, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")]


class LedgerReadback(ContractModel):
    """The last read-back of a line: ``found`` = the effect is visible in the target system."""

    at: AwareDatetime
    ok: StrictBool
    """The read-back query itself succeeded (``False``: it errored and says nothing)."""
    found: StrictBool
    detail: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def _found_needs_ok(self) -> LedgerReadback:
        if self.found and not self.ok:
            raise ValueError("a failed read-back cannot have found the effect")
        return self


class CommitLedgerEntry(ContractModel):
    """One line of a per-op commit (one tool call per ``(change_set_id, line_seq)``)."""

    change_set_id: Annotated[str, Field(min_length=1, max_length=200)]
    line_seq: PositiveInt
    op: OpName
    connector: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]
    tool: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
    idempotency: Idempotency
    state: CommitLedgerState
    attempts: Annotated[StrictInt, Field(ge=1, le=MAX_AUTO_ATTEMPTS)]
    request_id: Annotated[str, Field(min_length=1, max_length=200)] | None
    """The per-line idempotency key (``idempotency_key(...)`` + line suffix); sent under the
    mapping's ``request_id_arg``. Required for ``enforced`` lines and never changes on a retry."""
    ext_ref: Annotated[str, Field(max_length=200)] | None = None
    """What the target system returned (e.g. the created transfer order code)."""
    readback: LedgerReadback | None = None
    error: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _rules(self) -> CommitLedgerEntry:
        if self.idempotency is Idempotency.ENFORCED and self.request_id is None:
            raise ValueError("an enforced line carries its request_id")
        if self.attempts > 1:
            if self.idempotency is Idempotency.NONE:
                raise ValueError("idempotency 'none' lines are never auto-retried")
            if self.idempotency is Idempotency.BEST_EFFORT and (self.readback is None or not self.readback.ok):
                raise ValueError("a best_effort line is read back before any retry")
        if self.state == "failed" and self.error is None:
            raise ValueError("a failed line says why (error)")
        if self.state == "verified" and (self.readback is None or not self.readback.found):
            raise ValueError("a verified line has a read-back that found the effect")
        if self.state in ("attempted", "in_doubt") and self.readback is not None and self.readback.found:
            raise ValueError("a read-back that found the effect settles the line (succeeded/verified)")
        return self


def ledger_next_action(entry: CommitLedgerEntry) -> LedgerAction:
    """What the commit service does next with a line.

    ``none`` (settled) for succeeded / verified; ``guide`` for failed lines and for unsettled
    lines that may not be retried; otherwise ``readback`` or ``retry`` per idempotency (see the
    module docstring). A line that already used :data:`MAX_AUTO_ATTEMPTS` is guided."""
    if entry.state in ("succeeded", "verified"):
        return "none"
    if entry.state == "failed":
        return "guide"
    # attempted (crashed mid-call) or in_doubt: the outcome is unknown
    if entry.idempotency is Idempotency.ENFORCED:
        return "retry" if entry.attempts < MAX_AUTO_ATTEMPTS else "guide"
    rb = entry.readback
    if rb is None or not rb.ok:
        return "readback"
    # read back, effect not found (found=True is settled by the validator)
    if entry.idempotency is Idempotency.NONE:
        return "guide"
    return "retry" if entry.attempts < MAX_AUTO_ATTEMPTS else "guide"


def atomic_group_bulk_allowed(member_idempotencies: Iterable[Idempotency | str]) -> bool:
    """An atomic group on a tier-A connector may be bulk-applied per-op only when every member
    op is ``enforced`` (else the whole group is guided). An empty group is not allowed."""
    members = [Idempotency(i) for i in member_idempotencies]
    return bool(members) and all(i is Idempotency.ENFORCED for i in members)
