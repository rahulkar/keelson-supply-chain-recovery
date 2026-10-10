"""The sidecar's declarative configuration (``sidecar.yaml``, ``version: 1``; contract 122 §3).

One YAML file describes the system behind the sidecar: the backend (``csv`` / ``sql`` /
``rest``), its tables (typed columns, key, version column, which ``scn/1.0`` entities a row
feeds), one ``SELECT`` per ``scn/1.0`` entity, the ``list_transfer_orders`` view, the canonical
change-set ops as **write primitives** (``require`` / ``update`` / ``insert`` / ``adjust``, all
with bound parameters — never free SQL in writes), table invariants, operator-guide templates,
inbound auth keys and the receipt rules.

:func:`load_config` validates everything that can be checked without a database (fail fast):
identifiers, template placeholders per context (``keelson_contracts.changeset`` payload fields
for typed ops), referenced tables and columns, op names, no float literals, no inline secrets
(secrets are only ever named by ``*_env`` keys). The database-dependent checks (entity SQL
output columns == ``SCN_COLUMNS``) run at start-up and in ``python -m adapter_sidecar check``
(:mod:`adapter_sidecar.backends`).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from adapter_sidecar.errors import ConfigError
from adapter_sidecar.templates import Ref, RootRule, TemplateError, check_refs, parse_ref
from keelson_contracts.changeset import OP_NAME_PATTERN, OP_PAYLOAD_MODELS
from keelson_contracts.common import TENANT_SLUG_PATTERN
from keelson_contracts.mapping import ExpressionError, ScnEntityName, parse_path
from keelson_contracts.scn import SCN_ENTITIES

IDENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
TABLE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,62}(\.[A-Za-z_][A-Za-z0-9_]{0,62})?$"
ENV_PATTERN = r"^[A-Z][A-Z0-9_]{0,63}$"
ERROR_CODE_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
KEY_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,39}$"
HEADER_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
QUERY_PARAM_PATTERN = r"^[A-Za-z0-9_.\-\[\]]{1,64}$"
CODE_FORMAT_RE = re.compile(r"^([A-Za-z0-9_\-]*)\{n(?::0([1-9]|1[0-2])d)?\}([A-Za-z0-9_\-]*)$")
_SECRET_KEY_RE = re.compile(r"(secret|passw(or)?d|token|api[_-]?key|credential|private[_-]?key)", re.I)

SCOPE_CATALOG = "catalog:read"
SCOPE_ORDERS = "orders:read"
SCOPE_SNAPSHOT = "snapshot:read"
SCOPE_PREVIEW = "changes:preview"
SCOPE_APPLY = "changes:apply"
SCOPE_APPLY_AUTO = "changes:apply:auto"
SCOPES: tuple[str, ...] = (SCOPE_CATALOG, SCOPE_ORDERS, SCOPE_SNAPSHOT, SCOPE_PREVIEW, SCOPE_APPLY, SCOPE_APPLY_AUTO)
READ_SCOPES: tuple[str, ...] = (SCOPE_CATALOG, SCOPE_ORDERS, SCOPE_SNAPSHOT)

TRANSFER_ORDER_CREATE = "transfer_order.create"
CHANGE_SET_FIELDS = frozenset({"id", "code", "title"})

STATE_TABLES: frozenset[str] = frozenset(
    {"schema_migrations", "change_set", "change_set_item", "change_set_effect", "idempotency_record",
     "used_receipt", "audit_log", "journal", "source_fingerprint", "snapshot", "apply_journal", "sequence"}
)
"""Names of the sidecar's own state tables: a CSV / REST backend stages its tables in the state
database, so a configured table may not use one of these names."""

Ident = Annotated[str, Field(pattern=IDENT_PATTERN)]
TableName = Annotated[str, Field(pattern=TABLE_PATTERN)]
EnvName = Annotated[str, Field(pattern=ENV_PATTERN)]
ErrorCode = Annotated[str, Field(pattern=ERROR_CODE_PATTERN)]
OpName = Annotated[str, Field(pattern=OP_NAME_PATTERN, max_length=64)]
Scope = Literal["catalog:read", "orders:read", "snapshot:read", "changes:preview", "changes:apply",
                "changes:apply:auto"]
ColumnType = Literal["text", "int", "real", "date", "bool", "json"]
Scalar = str | int | bool | None
TemplateValue = Any
"""A JSON template (validated by :mod:`adapter_sidecar.templates` in context)."""
RefPath = Annotated[str, Field(min_length=1, max_length=200)]
"""A bare ref such as ``payload.lines`` (``for_each``: the list to iterate)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# ───────────────────────────── backend ─────────────────────────────


class CsvBackend(_Model):
    delimiter: Annotated[str, Field(min_length=1, max_length=1)] = ","
    encoding: Literal["utf-8", "utf-8-sig"] = "utf-8"


class SqlBackend(_Model):
    url_env: EnvName
    """Environment variable holding the SQLAlchemy async URL (``postgresql+asyncpg://…`` or
    ``sqlite+aiosqlite:///…``); the URL usually carries a password, so never inline."""
    read_only: bool = False
    apply_marker_table: TableName = "keelson_sidecar_apply"
    """Exactly-once marker: every apply inserts ``{change_set_id, preview_hash, applied_at}`` here
    in the same backend transaction, so an apply left in doubt by a crash is settled on restart."""
    manage_marker_table: bool = False
    """Create the marker table at start-up (customer-DB DDL; otherwise the DBA creates it)."""


class RestAuth(_Model):
    kind: Literal["none", "header", "oauth_client_credentials"] = "none"
    header: Annotated[str, Field(pattern=HEADER_PATTERN)] | None = None
    token_env: EnvName | None = None
    token_url: str | None = None
    client_id_env: EnvName | None = None
    client_secret_env: EnvName | None = None
    scope: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def _shape(self) -> RestAuth:
        if self.kind == "header" and not (self.header and self.token_env):
            raise ValueError("auth kind 'header' needs header and token_env")
        if self.kind == "oauth_client_credentials" and not (self.token_url and self.client_id_env
                                                             and self.client_secret_env):
            raise ValueError("auth kind 'oauth_client_credentials' needs token_url, client_id_env and "
                             "client_secret_env")
        if self.token_url is not None:
            _http_url(self.token_url, "token_url")
        return self


class RestBackend(_Model):
    base_url: str
    auth: RestAuth = RestAuth()
    max_pages: Annotated[int, Field(ge=1, le=100_000)] = 1000
    max_response_bytes: Annotated[int, Field(ge=1024, le=100_000_000)] = 10_000_000
    timeout_s: Annotated[int, Field(ge=1, le=300)] = 30
    min_crawl_interval_s: Annotated[int, Field(ge=0, le=86_400)] = 60
    """``scn.changes_since`` re-crawls at most this often to detect drift."""

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _http_url(value, "base_url").rstrip("/")


class BackendSpec(_Model):
    kind: Literal["csv", "sql", "rest"]
    csv: CsvBackend | None = None
    sql: SqlBackend | None = None
    rest: RestBackend | None = None

    @model_validator(mode="after")
    def _one(self) -> BackendSpec:
        others = {"csv", "sql", "rest"} - {self.kind}
        if any(getattr(self, o) is not None for o in others):
            raise ValueError(f"backend kind {self.kind!r} takes only the {self.kind!r} block")
        if self.kind == "sql" and self.sql is None:
            raise ValueError("backend kind 'sql' needs a 'sql' block (url_env)")
        if self.kind == "rest" and self.rest is None:
            raise ValueError("backend kind 'rest' needs a 'rest' block (base_url)")
        return self

    @property
    def csv_opts(self) -> CsvBackend:
        return self.csv or CsvBackend()


# ───────────────────────────── tables ─────────────────────────────


class ColumnSpec(_Model):
    type: ColumnType
    path: str | None = None
    """REST backends: JSONPath-lite of the value in one item (default ``$.<column>``)."""

    @field_validator("path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parse_path(value)
            except ExpressionError as exc:
                raise ValueError(f"path {value!r}: {exc}") from None
        return value


class Pagination(_Model):
    kind: Literal["page", "offset", "cursor", "link"]
    param: Annotated[str, Field(pattern=QUERY_PARAM_PATTERN)] | None = None
    """Query parameter carrying the page number / offset / cursor (not used by ``link``)."""
    size_param: Annotated[str, Field(pattern=QUERY_PARAM_PATTERN)] | None = None
    size: Annotated[int, Field(ge=1, le=1000)] = 100
    start: Annotated[int, Field(ge=0, le=1)] = 1
    """First page number (``page``: 0 or 1)."""
    cursor_path: str | None = None
    """``cursor``: JSONPath-lite of the next cursor in the body (missing / null = last page)."""
    next_path: str | None = None
    """``link``: JSONPath-lite of the next URL in the body; without it the ``Link: <…>; rel="next"``
    response header is used. A next URL must stay on the ``base_url`` origin."""

    @model_validator(mode="after")
    def _shape(self) -> Pagination:
        for name in ("cursor_path", "next_path"):
            value = getattr(self, name)
            if value is not None:
                try:
                    parse_path(value)
                except ExpressionError as exc:
                    raise ValueError(f"{name} {value!r}: {exc}") from None
        if self.kind in ("page", "offset", "cursor") and not self.param:
            raise ValueError(f"pagination {self.kind!r} needs param")
        if self.kind == "cursor" and not self.cursor_path:
            raise ValueError("pagination 'cursor' needs cursor_path")
        return self


class Endpoint(_Model):
    path: Annotated[str, Field(pattern=r"^/[A-Za-z0-9_.~/\-]*$", max_length=500)]
    params: dict[Annotated[str, Field(pattern=QUERY_PARAM_PATTERN)], str | int | bool] = Field(default_factory=dict)
    items_path: str = "$"
    pagination: Pagination | None = None

    @field_validator("items_path")
    @classmethod
    def _items(cls, value: str) -> str:
        try:
            parse_path(value)
        except ExpressionError as exc:
            raise ValueError(f"items_path {value!r}: {exc}") from None
        return value


class TableSpec(_Model):
    file: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}\.csv$")] | None = None
    endpoint: Endpoint | None = None
    columns: dict[Ident, ColumnType | ColumnSpec] = Field(min_length=1)
    key: Annotated[list[Ident], Field(min_length=1, max_length=8)]
    version_col: Ident | None = None
    affects: dict[ScnEntityName, Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=dict)
    """``scn/1.0`` entity → ``source_id`` template over this table's columns (``{{code}}``); every
    applied row change becomes one change-feed entry per affected entity."""
    fingerprint_sql: Annotated[str, Field(min_length=1, max_length=5000)] | None = None
    """SQL backends: a cheap query whose result changes when the table changes (e.g.
    ``SELECT count(*), max(rev) FROM stock``); drives external-drift detection."""

    @property
    def column_types(self) -> dict[str, ColumnType]:
        return {name: (spec if isinstance(spec, str) else spec.type) for name, spec in self.columns.items()}

    def column_path(self, name: str) -> str:
        spec = self.columns[name]
        return spec.path if isinstance(spec, ColumnSpec) and spec.path else f"$.{name}"

    @model_validator(mode="after")
    def _cols(self) -> TableSpec:
        types = self.column_types
        missing = [k for k in self.key if k not in types]
        if missing:
            raise ValueError(f"key columns {missing} are not columns")
        if len(set(self.key)) != len(self.key):
            raise ValueError("key columns repeat")
        if self.version_col is not None:
            if types.get(self.version_col) != "int":
                raise ValueError(f"version_col {self.version_col!r} must be an int column")
            if self.version_col in self.key:
                raise ValueError("version_col cannot be a key column")
        rules = {c: RootRule(frozenset()) for c in types}
        for entity, template in self.affects.items():
            try:
                check_refs(template, rules, f"affects.{entity}")
            except TemplateError as exc:
                raise ValueError(str(exc)) from None
        return self


class EntitySpec(_Model):
    sql: Annotated[str, Field(min_length=1, max_length=20_000)]
    """One ``SELECT`` producing exactly the entity's ``SCN_COLUMNS`` (by name); run in a read-only
    snapshot transaction."""


class TransferOrdersView(_Model):
    header_sql: Annotated[str, Field(min_length=1, max_length=20_000)]
    """Columns: ``code, from_dc, to_dc, ship_date, eta_date, status, created_via``."""
    lines_sql: Annotated[str, Field(min_length=1, max_length=20_000)]
    """Columns: ``code`` (of the transfer order), ``line_no, sku, qty``."""


class Views(_Model):
    transfer_orders: TransferOrdersView | None = None


VIEW_HEADER_COLUMNS = ("code", "from_dc", "to_dc", "ship_date", "eta_date", "status", "created_via")
VIEW_LINE_COLUMNS = ("code", "line_no", "sku", "qty")


# ───────────────────────────── ops ─────────────────────────────


KeyTemplate = Annotated[dict[Ident, TemplateValue], Field(min_length=1, max_length=8)]


class Target(_Model):
    table: TableName
    key: KeyTemplate


class UpdateStep(_Model):
    table: TableName
    key: KeyTemplate
    set: dict[Ident, TemplateValue] = Field(default_factory=dict)
    add: dict[Ident, TemplateValue] = Field(default_factory=dict)
    bump_version: bool = True
    for_each: RefPath | None = None

    @model_validator(mode="after")
    def _some(self) -> UpdateStep:
        if not self.set and not self.add:
            raise ValueError("update needs set and/or add")
        if set(self.set) & set(self.add):
            raise ValueError("a column is both set and added")
        return self


class InsertStep(_Model):
    table: TableName
    values: Annotated[dict[Ident, TemplateValue], Field(min_length=1)]
    for_each: RefPath | None = None


class AtLeast(_Model):
    expr: Annotated[dict[Ident, int], Field(min_length=1, max_length=16)]
    """Integer coefficients per int column: ``{on_hand: 1, allocated: -1, reserved: -1}``."""
    value: TemplateValue


class RequireStep(_Model):
    table: TableName
    key: KeyTemplate
    in_: dict[Ident, Annotated[list[Scalar], Field(min_length=1)]] = Field(default_factory=dict, alias="in")
    at_least: AtLeast | None = None
    error: ErrorCode = "precondition_failed"
    """Code of a failed ``in`` / ``at_least`` check."""
    missing_error: ErrorCode = "not_found"
    """Code when the row does not exist."""
    for_each: RefPath | None = None


class AdjustStep(_Model):
    table: TableName
    key: KeyTemplate
    add: Annotated[dict[Ident, TemplateValue], Field(min_length=1)]
    upsert: dict[Ident, TemplateValue] | None = None
    """Values of the non-key columns when the row does not exist yet (then it is inserted with
    ``add`` applied on top); without it a missing row fails with ``not_found``."""
    for_each: RefPath | None = None


class Step(_Model):
    require: RequireStep | None = None
    update: UpdateStep | None = None
    insert: InsertStep | None = None
    adjust: AdjustStep | None = None

    @model_validator(mode="after")
    def _one(self) -> Step:
        if sum(x is not None for x in (self.require, self.update, self.insert, self.adjust)) != 1:
            raise ValueError("each step is exactly one of require / update / insert / adjust")
        return self

    @property
    def kind(self) -> str:
        return next(k for k in ("require", "update", "insert", "adjust") if getattr(self, k) is not None)

    @property
    def body(self) -> RequireStep | UpdateStep | InsertStep | AdjustStep:
        return getattr(self, self.kind)  # type: ignore[no-any-return]


class CodeSpec(_Model):
    format: Annotated[str, Field(max_length=40)]
    """``TR-{n:05d}``: a prefix, ``{n}`` (optionally zero-padded) and a suffix."""
    table: TableName
    column: Ident
    """Existing codes in ``table.column`` are scanned so a new code never repeats one."""

    @field_validator("format")
    @classmethod
    def _format(cls, value: str) -> str:
        if not CODE_FORMAT_RE.match(value):
            raise ValueError("code format must look like PREFIX-{n:05d} (letters, digits, '-', '_')")
        return value

    def pattern(self) -> re.Pattern[str]:
        m = CODE_FORMAT_RE.match(self.format)
        assert m is not None
        return re.compile("^" + re.escape(m.group(1)) + r"(\d+)" + re.escape(m.group(3)) + "$")

    def render(self, n: int) -> str:
        return self.format.format(n=n)


class PriceSpec(_Model):
    table: TableName
    key: KeyTemplate
    column: Ident


class UnitsSpec(_Model):
    for_each: RefPath | None = None
    qty: TemplateValue = None
    delta: Ident | None = None
    """``abs(after - before)`` of this target column (e.g. an allocation quantity)."""
    price: PriceSpec | None = None
    """Unit value in cents (``value_cents`` = Σ units × price)."""

    @model_validator(mode="after")
    def _one(self) -> UnitsSpec:
        if (self.qty is None) == (self.delta is None):
            raise ValueError("units takes exactly one of qty / delta")
        if self.delta is not None and self.for_each is not None:
            raise ValueError("units.delta works on the target row (no for_each)")
        return self


class GuideList(_Model):
    for_each: RefPath
    item: Annotated[str, Field(min_length=1, max_length=500)]
    sep: Annotated[str, Field(max_length=10)] = ", "


class GuideSpec(_Model):
    title: Annotated[str, Field(min_length=1, max_length=500)]
    instructions: Annotated[str, Field(min_length=1, max_length=2000)]
    deep_link: Annotated[str, Field(max_length=2000)] | None = None
    expected: dict[str, TemplateValue] | None = None
    """``{entity_type, entity_ref, match}`` — what a read-back finds once the step is done."""
    list: GuideList | None = None
    """Renders ``{{list}}``: one ``item`` per element of ``for_each`` joined by ``sep``."""


class OpSpec(_Model):
    target: Target | None = None
    check_version: bool = False
    code: CodeSpec | None = None
    steps: Annotated[list[Step], Field(min_length=1, max_length=32)]
    units: UnitsSpec | None = None
    result_ref: TemplateValue = None
    """What ``result_ref`` records per applied item (default ``{{code}}`` or ``{{target_ref}}``)."""
    guide: GuideSpec | None = None

    @model_validator(mode="after")
    def _version(self) -> OpSpec:
        if self.check_version and self.target is None:
            raise ValueError("check_version needs a target")
        if self.units is not None and self.units.delta is not None and self.target is None:
            raise ValueError("units.delta needs a target")
        return self


class Invariant(_Model):
    expr: Annotated[dict[Ident, int], Field(min_length=1, max_length=16)]
    min: int = 0
    error: ErrorCode


# ───────────────────────────── auth / receipts ─────────────────────────────


class StaticKey(_Model):
    name: Annotated[str, Field(pattern=KEY_NAME_PATTERN)]
    key_env: EnvName
    scopes: Annotated[list[Scope], Field(min_length=1)]


class StaticHeaderAuth(_Model):
    header: Annotated[str, Field(pattern=HEADER_PATTERN)] = "X-Adapter-Key"
    keys: Annotated[list[StaticKey], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def _unique(self) -> StaticHeaderAuth:
        names = [k.name for k in self.keys]
        if len(set(names)) != len(names):
            raise ValueError("key names repeat")
        if self.header.lower() in ("authorization", "cookie", "host"):
            raise ValueError(f"header {self.header!r} is reserved")
        return self


class OAuthAuth(_Model):
    issuer: str
    jwks_url: str
    audience: str | None = None
    """Default: ``MCP_RESOURCE``."""
    scopes_claim: Ident = "scope"

    @model_validator(mode="after")
    def _urls(self) -> OAuthAuth:
        _http_url(self.issuer, "issuer")
        _http_url(self.jwks_url, "jwks_url")
        return self


class AuthSpec(_Model):
    static_header: StaticHeaderAuth | None = None
    oauth: OAuthAuth | None = None

    @model_validator(mode="after")
    def _some(self) -> AuthSpec:
        if self.static_header is None and self.oauth is None:
            raise ValueError("auth needs static_header and/or oauth")
        return self


class AutocommitSpec(_Model):
    ops: Annotated[list[OpName], Field(min_length=1)]
    max_items: Annotated[int, Field(ge=1, le=500)] = 50
    max_units: Annotated[int, Field(ge=0)] = 5000


class ReceiptsSpec(_Model):
    tenants: list[Annotated[str, Field(pattern=TENANT_SLUG_PATTERN)]] = Field(default_factory=list)
    """Keelson tenants whose receipts are accepted (empty = any; the sidecar is single-tenant)."""
    expected_ext_sub: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    """``ext_sub`` the receipt must carry (Keelson sends ``connector:<slug>`` for non-platform
    connectors); unset = not checked."""
    ext_iss: str | None = None
    """``ext_iss`` the receipt must carry when present (default: the origin of ``MCP_RESOURCE``)."""
    autocommit: AutocommitSpec | None = None
    """Caps for policy (``approved_by: policy:…``) receipts — defence in depth on top of Keelson's
    policy; unset = policy receipts are refused."""


class SnapshotSpec(_Model):
    part_rows: Annotated[int, Field(ge=1, le=1_000_000)] = 50_000
    ttl_s: Annotated[int, Field(ge=60, le=86_400)] = 7200


class ChangesSpec(_Model):
    retention: Annotated[int, Field(ge=100, le=10_000_000)] = 10_000
    """Journal rows kept; a watermark older than the oldest kept row asks for a full resync."""


class SystemSpec(_Model):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    vendor: Annotated[str, Field(min_length=1, max_length=100)]


# ───────────────────────────── root ─────────────────────────────


class SidecarConfig(_Model):
    version: Literal[1]
    system: SystemSpec
    backend: BackendSpec
    tables: dict[TableName, TableSpec] = Field(default_factory=dict)
    entities: dict[ScnEntityName, EntitySpec] = Field(default_factory=dict)
    views: Views = Views()
    ops: dict[OpName, OpSpec] = Field(default_factory=dict)
    invariants: dict[TableName, list[Invariant]] = Field(default_factory=dict)
    auth: AuthSpec
    receipts: ReceiptsSpec = ReceiptsSpec()
    snapshot: SnapshotSpec = SnapshotSpec()
    changes: ChangesSpec = ChangesSpec()

    @property
    def writable(self) -> bool:
        """Tier W is offered: ops are configured and the backend accepts writes."""
        if not self.ops or self.backend.kind == "rest":
            return False
        return not (self.backend.kind == "sql" and self.backend.sql is not None and self.backend.sql.read_only)

    @property
    def consistency(self) -> Literal["consistent", "crawled"]:
        return "crawled" if self.backend.kind == "rest" else "consistent"

    def table(self, name: str) -> TableSpec:
        return self.tables[name]

    @model_validator(mode="after")
    def _cross(self) -> SidecarConfig:
        problems = config_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


# ───────────────────────────── cross checks ─────────────────────────────


def _http_url(value: str, name: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if "@" in parts.netloc:
        raise ValueError(f"{name} must not carry userinfo (put credentials in *_env variables)")
    if parts.fragment or parts.query:
        raise ValueError(f"{name} must not carry a query or fragment")
    return value


def _payload_fields(op: str) -> frozenset[str] | None:
    model = OP_PAYLOAD_MODELS.get(op)
    return frozenset(model.model_fields) if model is not None else None


def _item_fields(op: str, for_each: str) -> frozenset[str] | None:
    """Fields of the elements of a typed op's list payload field (``payload.lines`` → sku, qty)."""
    model = OP_PAYLOAD_MODELS.get(op)
    ref = parse_ref(for_each)
    if model is None or ref.root != "payload" or len(ref.path) != 1:
        return None
    field = model.model_fields.get(str(ref.path[0]))
    if field is None:
        return None
    for arg in get_args(field.annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return frozenset(arg.model_fields)
    return None


def config_problems(cfg: SidecarConfig) -> list[str]:
    """Every static problem of ``cfg`` (empty list = valid)."""
    problems: list[str] = []
    kind = cfg.backend.kind
    tables = cfg.tables
    for name, t in tables.items():
        where = f"tables.{name}"
        if kind in ("csv", "rest"):
            if "." in name:
                problems.append(f"{where}: {kind} tables have no schema prefix")
            if name.lower() in STATE_TABLES or name.lower().startswith("sqlite_"):
                problems.append(f"{where}: {name!r} is reserved for the sidecar's state tables")
        if kind == "csv" and t.file is None:
            problems.append(f"{where}: csv tables need file")
        if kind != "csv" and t.file is not None:
            problems.append(f"{where}: file is only for csv backends")
        if kind == "rest" and t.endpoint is None:
            problems.append(f"{where}: rest tables need endpoint")
        if kind != "rest" and t.endpoint is not None:
            problems.append(f"{where}: endpoint is only for rest backends")
        if kind != "sql" and t.fingerprint_sql is not None:
            problems.append(f"{where}: fingerprint_sql is only for sql backends")
        if kind != "rest" and any(isinstance(s, ColumnSpec) and s.path for s in t.columns.values()):
            problems.append(f"{where}: column paths are only for rest backends")
    if kind == "csv":
        files = [t.file for t in tables.values()]
        if len(set(files)) != len(files):
            problems.append("tables: two tables share a file")
    if kind == "rest" and cfg.ops:
        problems.append("ops: the rest backend is read-only in M8 (REST writes are deferred)")
    if kind == "sql" and cfg.backend.sql is not None and cfg.backend.sql.read_only and cfg.ops:
        problems.append("ops: backend.sql.read_only is set, so no ops can be configured")

    views = cfg.views
    if TRANSFER_ORDER_CREATE in cfg.ops and views.transfer_orders is None:
        problems.append("views.transfer_orders is required when transfer_order.create is configured "
                        "(Keelson reconciles transfer orders with list_transfer_orders)")

    for name, invs in cfg.invariants.items():
        t = tables.get(name)
        if t is None:
            problems.append(f"invariants.{name}: unknown table")
            continue
        for inv in invs:
            problems += _int_cols(t, inv.expr, f"invariants.{name}")

    mutated: set[str] = set()
    for op, spec in cfg.ops.items():
        problems += _op_problems(cfg, op, spec, mutated)
    for name in sorted(mutated):
        if name in tables and not tables[name].affects:
            problems.append(f"tables.{name}: ops change this table, so it needs affects "
                            "(the change feed would miss it)")

    receipts = cfg.receipts
    if receipts.autocommit is not None:
        unknown = sorted(set(receipts.autocommit.ops) - set(cfg.ops))
        if unknown:
            problems.append(f"receipts.autocommit.ops: {unknown} are not configured ops")
    if receipts.ext_iss is not None:
        try:
            _http_url(receipts.ext_iss, "receipts.ext_iss")
        except ValueError as exc:
            problems.append(str(exc))
    if cfg.ops and cfg.auth.static_header is not None:
        scopes = {s for k in cfg.auth.static_header.keys for s in k.scopes}
        if not scopes & {SCOPE_APPLY, SCOPE_APPLY_AUTO}:
            problems.append("auth: ops are configured but no key holds changes:apply or changes:apply:auto")
    return problems


def _int_cols(t: TableSpec, expr: Mapping[str, int], where: str) -> list[str]:
    types = t.column_types
    bad = [c for c in expr if types.get(c) != "int"]
    return [f"{where}: {bad} are not int columns"] if bad else []


def _op_problems(cfg: SidecarConfig, op: str, spec: OpSpec, mutated: set[str]) -> list[str]:
    where = f"ops.{op}"
    problems: list[str] = []
    if op.startswith("revert."):
        return [f"{where}: revert.* ops are internal (compensating change sets)"]
    tables = cfg.tables
    payload_fields = _payload_fields(op)
    base: dict[str, RootRule] = {
        "payload": RootRule(payload_fields),
        "target_ref": RootRule(frozenset()),
        "expected_version": RootRule(frozenset()),
        "op": RootRule(frozenset()),
        "change_set": RootRule(CHANGE_SET_FIELDS),
    }
    if spec.code is not None:
        base["code"] = RootRule(frozenset())
        if spec.code.table not in tables:
            problems.append(f"{where}.code: unknown table {spec.code.table!r}")
        elif tables[spec.code.table].column_types.get(spec.code.column) != "text":
            problems.append(f"{where}.code: {spec.code.table}.{spec.code.column} must be a text column")
    if spec.target is not None:
        t = tables.get(spec.target.table)
        if t is None:
            problems.append(f"{where}.target: unknown table {spec.target.table!r}")
        else:
            base["before"] = RootRule(frozenset(t.column_types))
            problems += _key_problems(t, spec.target.key, f"{where}.target")
            if spec.check_version and t.version_col is None:
                problems.append(f"{where}: check_version needs a version_col on {spec.target.table}")

    def roots_for(for_each: str | None, ctx: str) -> dict[str, RootRule] | None:
        if for_each is None:
            return base
        try:
            ref = parse_ref(for_each)
        except TemplateError as exc:
            problems.append(f"{ctx}.for_each: {exc}")
            return None
        if ref.root != "payload" or not ref.path:
            problems.append(f"{ctx}.for_each must name a payload field (e.g. payload.lines)")
            return None
        if payload_fields is not None and str(ref.path[0]) not in payload_fields:
            problems.append(f"{ctx}.for_each: {op} payload has no field {ref.path[0]!r}")
            return None
        return {**base, "item": RootRule(_item_fields(op, for_each)), "index": RootRule(frozenset())}

    def check(template: Any, roots: Mapping[str, RootRule], ctx: str) -> list[Ref]:
        try:
            return check_refs(template, roots, ctx)
        except TemplateError as exc:
            problems.append(str(exc))
            return []

    for i, step in enumerate(spec.steps):
        body = step.body
        ctx = f"{where}.steps[{i}].{step.kind}"
        t = tables.get(body.table)
        if t is None:
            problems.append(f"{ctx}: unknown table {body.table!r}")
            continue
        roots = roots_for(body.for_each, ctx)
        if roots is None:
            continue
        types = t.column_types
        if isinstance(body, InsertStep):
            mutated.add(body.table)
            unknown = sorted(set(body.values) - set(types))
            if unknown:
                problems.append(f"{ctx}: {unknown} are not columns of {body.table}")
            missing = [k for k in t.key if k not in body.values]
            if missing:
                problems.append(f"{ctx}: values must set the key columns {missing}")
            check(body.values, roots, ctx)
            continue
        problems += _key_problems(t, body.key, ctx)
        check(body.key, roots, f"{ctx}.key")
        if isinstance(body, RequireStep):
            unknown = sorted(set(body.in_) - set(types))
            if unknown:
                problems.append(f"{ctx}.in: {unknown} are not columns of {body.table}")
            if body.at_least is not None:
                problems += _int_cols(t, body.at_least.expr, f"{ctx}.at_least")
                check(body.at_least.value, roots, f"{ctx}.at_least.value")
        elif isinstance(body, UpdateStep):
            mutated.add(body.table)
            cols = {*body.set, *body.add}
            unknown = sorted(cols - set(types))
            if unknown:
                problems.append(f"{ctx}: {unknown} are not columns of {body.table}")
            fixed = sorted(cols & ({*t.key} | ({t.version_col} if t.version_col else set())))
            if fixed:
                problems.append(f"{ctx}: key / version columns {fixed} cannot be changed")
            problems += _int_cols(t, body.add, f"{ctx}.add")
            check(body.set, roots, f"{ctx}.set")
            check(body.add, roots, f"{ctx}.add")
        elif isinstance(body, AdjustStep):
            mutated.add(body.table)
            problems += _int_cols(t, body.add, f"{ctx}.add")
            if t.version_col is not None and t.version_col in body.add:
                problems.append(f"{ctx}: the version column is maintained by the sidecar")
            check(body.add, roots, f"{ctx}.add")
            if body.upsert is not None:
                unknown = sorted(set(body.upsert) - set(types))
                if unknown:
                    problems.append(f"{ctx}.upsert: {unknown} are not columns of {body.table}")
                check(body.upsert, roots, f"{ctx}.upsert")

    if spec.units is not None:
        u = spec.units
        roots = roots_for(u.for_each, f"{where}.units")
        if roots is not None:
            if u.qty is not None:
                check(u.qty, roots, f"{where}.units.qty")
            if u.delta is not None and spec.target is not None and spec.target.table in tables:
                if tables[spec.target.table].column_types.get(u.delta) != "int":
                    problems.append(f"{where}.units.delta: {u.delta!r} is not an int column of {spec.target.table}")
            if u.price is not None:
                pt = tables.get(u.price.table)
                if pt is None:
                    problems.append(f"{where}.units.price: unknown table {u.price.table!r}")
                else:
                    problems += _key_problems(pt, u.price.key, f"{where}.units.price")
                    if pt.column_types.get(u.price.column) != "int":
                        problems.append(f"{where}.units.price: {u.price.column!r} is not an int column")
                    check(u.price.key, roots, f"{where}.units.price.key")
    if spec.result_ref is not None:
        check(spec.result_ref, base, f"{where}.result_ref")
    if spec.guide is not None:
        g = spec.guide
        guide_roots = {k: v for k, v in base.items() if k not in ("code", "before")}
        guide_roots["result_ref"] = RootRule(frozenset())
        if g.list is not None:
            item_roots = roots_for(g.list.for_each, f"{where}.guide.list")
            if item_roots is not None:
                check(g.list.item, item_roots, f"{where}.guide.list.item")
            guide_roots["list"] = RootRule(frozenset())
        for name in ("title", "instructions", "deep_link", "expected"):
            value = getattr(g, name)
            if value is not None:
                check(value, guide_roots, f"{where}.guide.{name}")
    return problems


def _key_problems(t: TableSpec, key: Mapping[str, Any], where: str) -> list[str]:
    if set(key) != set(t.key):
        return [f"{where}: key must name exactly the table key columns {t.key} (got {sorted(key)})"]
    return []


# ───────────────────────────── loading ─────────────────────────────


def _raw_problems(node: Any, path: str = "") -> list[str]:
    """Float literals, YAML dates and inline secrets anywhere in the raw document."""
    out: list[str] = []
    if isinstance(node, bool) or node is None or isinstance(node, int | str):
        return out
    if isinstance(node, float):
        return [f"{path or '/'}: float literal {node!r} (use integers: cents, basis points, seconds)"]
    if isinstance(node, dt.date | dt.datetime):
        return [f"{path or '/'}: unquoted YAML date {node!s} (quote it: '{node!s}')"]
    if isinstance(node, list):
        for i, v in enumerate(node):
            out += _raw_problems(v, f"{path}/{i}")
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            key = str(k)
            # ``*_url`` keys (``token_url``) name an endpoint, not a credential; the URL fields refuse
            # userinfo and query strings (``_http_url``), so no secret can ride along in them.
            if _SECRET_KEY_RE.search(key) and not key.endswith(("_env", "_url")) and isinstance(v, str | int):
                out.append(f"{path}/{key}: secrets are never inline; name an environment variable with {key}_env")
            out += _raw_problems(v, f"{path}/{key}")
        return out
    return [f"{path or '/'}: unsupported YAML value of type {type(node).__name__}"]


def parse_config(raw: Any, *, source: str = "config") -> SidecarConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: the document must be a mapping")
    problems = _raw_problems(raw)
    if problems:
        raise ConfigError(f"{source}: " + "; ".join(problems))
    try:
        return SidecarConfig.model_validate(raw)
    except ValidationError as exc:
        lines = []
        for err in exc.errors()[:20]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            lines.append(f"{loc or '/'}: {err.get('msg')}")
        raise ConfigError(f"{source}: " + "; ".join(lines)) from None


def load_config(path: str | Path) -> SidecarConfig:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc.strerror}") from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML ({exc})") from None
    return parse_config(raw, source=str(p))


__all__ = [
    "SCN_ENTITIES",
    "SCOPES",
    "AdjustStep",
    "InsertStep",
    "OpSpec",
    "RequireStep",
    "SidecarConfig",
    "TableSpec",
    "UpdateStep",
    "load_config",
    "parse_config",
]
