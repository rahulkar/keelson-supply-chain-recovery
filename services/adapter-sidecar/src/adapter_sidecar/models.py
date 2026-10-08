"""Pydantic models of the MCP tool outputs (their JSON Schemas are advertised as output schemas).

Shapes follow Merxa's tools (contracts 20 / 22) so Keelson's commit path, sync and reconcile read
them unchanged; outputs allow extra keys (vendors may add fields)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class OriginIn(BaseModel):
    """Where a change set comes from (free-form; integers / strings only)."""

    model_config = ConfigDict(extra="allow")
    system: str | None = Field(None, description="calling system, e.g. 'Keelson'")
    case_ref: str | None = Field(None, description="caller's case reference")
    plan_version: int | str | None = Field(None, description="caller's plan version")


class SystemInfoOut(_Out):
    system: str
    vendor: str
    backend: Literal["csv", "sql", "rest"]
    consistency: Literal["consistent", "crawled"]
    canonical_model: Literal["scn/1.0"]
    entities: dict[str, Literal["mapped", "empty"]]
    ops: list[str]
    tiers_offered: list[str]


class SnapshotOpenOut(_Out):
    snapshot_id: str
    as_of: str
    watermark: str
    canonical_model: Literal["scn/1.0"]
    expires_at: str
    consistency: Literal["consistent", "crawled"]
    as_of_range: dict[str, str] | None = None
    entities: dict[str, dict[str, Any]]


class SnapshotPart(_Out):
    uri: str
    sha256: str
    rows: int
    bytes: int


class SnapshotExportOut(_Out):
    snapshot_id: str
    entity: str
    rows: int
    parts: list[SnapshotPart]


class ChangesOut(_Out):
    changes: list[dict[str, Any]]
    next_watermark: str
    has_more: bool
    full_resync_required: bool | None = None


class ChangeSetOut(_Out):
    change_set_id: str
    code: str
    title: str
    status: str
    items_count: int
    preview_hash: str | None = None
    attribution_text: str | None = None
    items: list[dict[str, Any]]


class AuditOut(_Out):
    items: list[dict[str, Any]]


class TransferOrdersOut(_Out):
    items: list[dict[str, Any]]


class GuideExpected(_Out):
    entity_type: str
    entity_ref: str | None = None
    match: dict[str, Any]


class GuideStep(_Out):
    seq: int
    item_seq: int
    op: str | None = None
    target_ref: str | None = None
    title: str
    instructions: str
    deep_link: str | None = None
    expected: dict[str, Any] | None = None


class GuideOut(_Out):
    change_set_id: str
    code: str
    steps: list[GuideStep]


class CreateOut(_Out):
    change_set_id: str
    code: str
    status: Literal["draft"]


class AddItemsOut(_Out):
    change_set_id: str
    items_count: int


class ValidatedItem(_Out):
    seq: int
    op: str
    target_ref: str | None
    before: Any = None
    after: Any = None
    ok: bool
    errors: list[str]


class Totals(_Out):
    lines: int
    units_moved: int
    value_cents: int


class ValidateOut(_Out):
    change_set_id: str
    code: str
    status: Literal["validated", "invalid"]
    preview_hash: str
    items: list[ValidatedItem]
    totals: Totals
    atomic: bool


class ConflictRow(_Out):
    seq: int
    target_ref: str | None
    expected_version: int | None
    actual_version: int | None
    reason: str


class ApplyOut(_Out):
    status: Literal["applied", "conflict"]
    applied: int | None = None
    audit_ref: str | None = None
    change_set_code: str | None = None
    change_set_id: str | None = None
    conflicts: list[ConflictRow] | None = None


class RollbackOut(_Out):
    status: Literal["rolled_back", "conflict"]
    change_set_id: str | None = None
    change_set_code: str | None = None
    compensating_change_set_id: str | None = None
    compensating_code: str | None = None
    applied: int | None = None
    audit_ref: str | None = None
    conflicts: list[ConflictRow] | None = None
