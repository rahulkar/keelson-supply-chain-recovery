"""Connector tool classification and capability tiers (plan §10).

Classifications are admin-confirmed per ``connector_tool`` row and drive binding:
LLM investigators only ever receive ``read`` + ``trusted_data`` tools; writes go
through deterministic nodes behind an approval gate.

M7 (``110`` §4): :class:`ConnectorCapabilities` summarises what the engine can do with a
connector (sync kind, snapshot consistency, commit mode, mapped ops) for the admin badges and
the planner; build it with :func:`summarize_capabilities`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field, StrictBool, model_validator

from .common import ContractModel, PositiveInt
from .scn import SnapshotConsistency

__all__ = [
    "CommitMode",
    "ConnectorCapabilities",
    "SyncKind",
    "summarize_capabilities",
    "ConnectorTier",
    "ContentTrust",
    "Egress",
    "Idempotency",
    "TIER_LABELS",
    "ToolAccess",
    "ToolClassification",
    "detect_tiers",
]


class ToolAccess(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class ContentTrust(StrEnum):
    TRUSTED_DATA = "trusted_data"
    UNTRUSTED_TEXT = "untrusted_text"
    """Free text from third parties (supplier messages, news): never steers control flow."""


class Egress(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class Idempotency(StrEnum):
    ENFORCED = "enforced"
    BEST_EFFORT = "best_effort"
    NONE = "none"


class ToolClassification(ContractModel):
    access: ToolAccess
    content_trust: ContentTrust
    egress: Egress
    idempotency: Idempotency

    @property
    def investigator_bindable(self) -> bool:
        """May be bound to an LLM investigator (read-only, trusted structured data)."""
        return self.access is ToolAccess.READ and self.content_trust is ContentTrust.TRUSTED_DATA

    @property
    def requires_approval(self) -> bool:
        """Writes and destructive tools only run from deterministic nodes after approval."""
        return self.access is not ToolAccess.READ

    @property
    def auto_executable(self) -> bool:
        """Automation (Tier A action mapping) needs replay safety; else fall back to Guide."""
        return self.idempotency is not Idempotency.NONE


class ConnectorTier(StrEnum):
    A = "A"
    """Agent: plain read/write tools (crawl sync, advisory)."""
    B = "B"
    """Snapshot: ``scn.snapshot_open/export``."""
    C = "C"
    """Change feed: ``scn.changes_since`` and/or webhooks."""
    W = "W"
    """Change-sets: create/add/validate/apply/rollback + operator guide."""


TIER_LABELS: Final[Mapping[ConnectorTier, str]] = {
    ConnectorTier.A: "Agent",
    ConnectorTier.B: "Snapshot",
    ConnectorTier.C: "Change feed",
    ConnectorTier.W: "Change-sets",
}

_TIER_W_TOOLS: Final = frozenset(
    {"create_change_set", "add_change_set_items", "validate_change_set", "apply_change_set"}
)
_TIER_B_TOOLS: Final = frozenset({"scn.snapshot_open", "scn.snapshot_export"})
_TIER_C_TOOLS: Final = frozenset({"scn.changes_since"})


def detect_tiers(tool_names: Iterable[str], *, has_webhooks: bool = False) -> list[ConnectorTier]:
    """Capability badges for a connector from its ``tools/list`` names (sorted A, B, C, W).

    Every connector with at least one tool is tier A; B/C/W are added when the
    full tool set for that capability is present (``rollback_change_set`` is M5
    and not required for W).
    """
    names = set(tool_names)
    tiers: list[ConnectorTier] = []
    if names:
        tiers.append(ConnectorTier.A)
    if _TIER_B_TOOLS <= names:
        tiers.append(ConnectorTier.B)
    if _TIER_C_TOOLS <= names or (has_webhooks and names):
        tiers.append(ConnectorTier.C)
    if _TIER_W_TOOLS <= names:
        tiers.append(ConnectorTier.W)
    return tiers


# ───────────────────────────── M7 capability summary ─────────────────────────

SyncKind = Literal["snapshot", "crawl", "none"]
"""``snapshot`` (tier B export), ``crawl`` (tier A + read mapping), ``none``."""
CommitMode = Literal["change_set", "per_op", "guide"]
"""``change_set`` (tier W), ``per_op`` (tier A + action mapping; unmapped ops are guided),
``guide`` (nothing can be applied: every op is a guided step)."""

_OP_RE: Final = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class ConnectorCapabilities(ContractModel):
    """What the engine does with a connector (``110`` §4). ``advisory`` is true exactly when the
    data comes from a crawl (plans built on it are labelled advisory)."""

    connector: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]
    tiers: list[ConnectorTier]
    sync: SyncKind
    consistency: SnapshotConsistency | None
    commit: CommitMode
    read_mapping_version: PositiveInt | None
    action_mapping_version: PositiveInt | None
    mapped_ops: list[Annotated[str, Field(max_length=64, pattern=_OP_RE)]]
    advisory: StrictBool

    @model_validator(mode="after")
    def _consistent(self) -> ConnectorCapabilities:
        order = list(ConnectorTier)
        if self.tiers != sorted(set(self.tiers), key=order.index):
            raise ValueError("tiers are unique and in A, B, C, W order")
        if self.mapped_ops != sorted(set(self.mapped_ops)):
            raise ValueError("mapped_ops are unique and sorted")
        expected = summarize_capabilities(
            self.connector,
            self.tiers,
            read_mapping_version=self.read_mapping_version,
            action_mapping_version=self.action_mapping_version,
            mapped_ops=self.mapped_ops,
            _validate=False,
        )
        for field in ("sync", "consistency", "commit", "advisory"):
            if getattr(self, field) != getattr(expected, field):
                raise ValueError(f"{field} must be {getattr(expected, field)!r} for these tiers and mappings")
        if self.action_mapping_version is None and self.mapped_ops:
            raise ValueError("mapped_ops need an action mapping")
        return self


def summarize_capabilities(
    connector: str,
    tiers: Iterable[ConnectorTier | str],
    *,
    read_mapping_version: int | None = None,
    action_mapping_version: int | None = None,
    mapped_ops: Iterable[str] = (),
    _validate: bool = True,
) -> ConnectorCapabilities:
    """Derive the summary: tier B → consistent snapshot sync, else tier A with a read mapping →
    crawl (advisory); tier W → change sets, else tier A with an action mapping that maps at least
    one op → per-op commit, else guide."""
    tier_set = {ConnectorTier(t) for t in tiers}
    ordered = [t for t in ConnectorTier if t in tier_set]
    ops = sorted(set(mapped_ops))
    sync: SyncKind
    consistency: SnapshotConsistency | None
    if ConnectorTier.B in tier_set:
        sync, consistency = "snapshot", "consistent"
    elif ConnectorTier.A in tier_set and read_mapping_version is not None:
        sync, consistency = "crawl", "crawled"
    else:
        sync, consistency = "none", None
    commit: CommitMode
    if ConnectorTier.W in tier_set:
        commit = "change_set"
    elif ConnectorTier.A in tier_set and action_mapping_version is not None and ops:
        commit = "per_op"
    else:
        commit = "guide"
    data = {
        "connector": connector,
        "tiers": ordered,
        "sync": sync,
        "consistency": consistency,
        "commit": commit,
        "read_mapping_version": read_mapping_version,
        "action_mapping_version": action_mapping_version,
        "mapped_ops": ops,
        "advisory": consistency == "crawled",
    }
    if not _validate:
        return ConnectorCapabilities.model_construct(**data)  # type: ignore[arg-type]
    return ConnectorCapabilities.model_validate(data)
