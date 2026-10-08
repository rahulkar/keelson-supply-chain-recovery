"""SQLAlchemy Core table metadata mirroring ``migrations/*.sql``.

The SQL migrations are the source of truth for DDL (constraints, triggers, grants);
these definitions exist for typed query building only and are never used to create
tables.
"""

from __future__ import annotations

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import DATERANGE, JSONB

SCHEMA = "merxa"
metadata = MetaData(schema=SCHEMA)


def _versioned() -> list[Column]:
    return [
        Column("version", Integer, nullable=False, server_default="1"),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    ]


market = Table(
    "market",
    metadata,
    Column("code", Text, primary_key=True),
    Column("id", Uuid, server_default="uuidv7()"),
    Column("name", Text, nullable=False),
    Column("currency", Text, nullable=False),
    *_versioned(),
)

dc = Table(
    "dc",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("city", Text, nullable=False),
    Column("lat", Float, nullable=False),
    Column("lon", Float, nullable=False),
    Column("inbound_cap_units_wk", Integer, nullable=False),
    Column("outbound_cap_units_wk", Integer, nullable=False),
    *_versioned(),
)

store_cluster = Table(
    "store_cluster",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("dc_code", Text, nullable=False),
    Column("climate", Text, nullable=False),
    Column("grade_band", Text, nullable=False),
    *_versioned(),
)

store = Table(
    "store",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("market_code", Text, nullable=False),
    Column("city", Text, nullable=False),
    Column("lat", Float, nullable=False),
    Column("lon", Float, nullable=False),
    Column("grade", Text, nullable=False),
    Column("cluster_code", Text, nullable=False),
    Column("home_dc_code", Text, nullable=False),
    Column("sales_index", Numeric(6, 3), nullable=False),
    *_versioned(),
)

supplier = Table(
    "supplier",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("kind", Text, nullable=False),
    *_versioned(),
)

factory = Table(
    "factory",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("supplier_code", Text, nullable=False),
    *_versioned(),
)

component = Table(
    "component",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("supplier_code", Text, nullable=False),
    *_versioned(),
)

style = Table(
    "style",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("family", Text, nullable=False),
    Column("season", Text, nullable=False),
    Column("colour", Text, nullable=False),
    Column("retail_price_cents", BigInteger, nullable=False),
    Column("unit_cost_cents", BigInteger, nullable=False),
    *_versioned(),
)

bom_line = Table(
    "bom_line",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("style_code", Text, nullable=False),
    Column("component_code", Text, nullable=False),
    Column("factory_code", Text, nullable=True),
    Column("qty_per_unit", Integer, nullable=False),
    *_versioned(),
)

sku = Table(
    "sku",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("style_code", Text, nullable=False),
    Column("size", Text, nullable=False),
    Column("size_seq", Integer, nullable=False),
    *_versioned(),
)

size_curve = Table(
    "size_curve",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("family", Text, nullable=False),
    Column("cluster_code", Text, nullable=False),
    Column("size", Text, nullable=False),
    Column("share_bp", Integer, nullable=False),
    *_versioned(),
)

purchase_order = Table(
    "purchase_order",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("supplier_code", Text, nullable=False),
    Column("factory_code", Text, nullable=False),
    Column("dest_dc_code", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("ex_factory_date", Date, nullable=False),
    Column("eta_dc", Date, nullable=False),
    *_versioned(),
)

po_line = Table(
    "po_line",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("po_code", Text, nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("sku_code", Text, nullable=False),
    Column("qty_ordered", Integer, nullable=False),
    Column("qty_received", Integer, nullable=False, server_default="0"),
    Column("eta_dc", Date, nullable=False),
    Column("status", Text, nullable=False),
    *_versioned(),
)

inventory_position = Table(
    "inventory_position",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("location_type", Text, nullable=False),
    Column("location_code", Text, nullable=False),
    Column("sku_code", Text, nullable=False),
    Column("on_hand", Integer, nullable=False, server_default="0"),
    Column("allocated", Integer, nullable=False, server_default="0"),
    Column("reserved", Integer, nullable=False, server_default="0"),
    Column("in_transit", Integer, nullable=False, server_default="0"),
    *_versioned(),
)

demand_forecast = Table(
    "demand_forecast",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("style_code", Text, nullable=False),
    Column("cluster_code", Text, nullable=False),
    Column("store_code", Text, nullable=True),
    Column("iso_week", Text, nullable=False),
    Column("units", Integer, nullable=False),
    *_versioned(),
)

allocation = Table(
    "allocation",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("sku_code", Text, nullable=False),
    Column("dc_code", Text, nullable=False),
    Column("store_code", Text, nullable=False),
    Column("ship_week", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("status", Text, nullable=False),
    *_versioned(),
)

transfer_order = Table(
    "transfer_order",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("from_dc", Text, nullable=False),
    Column("to_dc", Text, nullable=False),
    Column("ship_date", Date, nullable=False),
    Column("eta_date", Date, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_via", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    *_versioned(),
)

transfer_order_line = Table(
    "transfer_order_line",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("to_code", Text, nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("sku_code", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    *_versioned(),
)

supplier_message = Table(
    "supplier_message",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("supplier_code", Text, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("subject", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="new"),
    *_versioned(),
)

change_set = Table(
    "change_set",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("origin", JSONB, nullable=False),
    Column("items_count", Integer, nullable=False, server_default="0"),
    Column("preview", JSONB, nullable=True),
    Column("preview_hash", Text, nullable=True),
    Column("result", JSONB, nullable=True),
    Column("approval_ref", Text, nullable=True),
    Column("receipt_jti", Text, nullable=True),
    Column("approved_by", Text, nullable=True),
    Column("initiated_by", Text, nullable=True),
    Column("applied_by_sub", Text, nullable=True),
    Column("applied_by_username", Text, nullable=True),
    Column("applied_by_display", Text, nullable=True),
    Column("applied_via_client", Text, nullable=True),
    Column("via", Text, nullable=True),
    Column("idempotency_key", Text, nullable=True),
    Column("created_by_client", Text, nullable=True),
    Column("created_by_sub", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("validated_at", DateTime(timezone=True), nullable=True),
    Column("applied_at", DateTime(timezone=True), nullable=True),
    Column("rollback_of", Uuid, nullable=True),
    Column("co_approvers", JSONB, nullable=True),  # M5 (0003): [{ref, name}] from the receipt
    *_versioned(),
)

change_set_item = Table(
    "change_set_item",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("change_set_id", Uuid, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("op", Text, nullable=False),
    Column("target_ref", Text, nullable=True),
    Column("expected_version", Integer, nullable=True),
    Column("payload", JSONB, nullable=False),
    Column("before", JSONB, nullable=True),
    Column("after", JSONB, nullable=True),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("error", Text, nullable=True),
    Column("result", JSONB, nullable=True),
    *_versioned(),
)

idempotency_record = Table(
    "idempotency_record",
    metadata,
    Column("principal", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("request_sha256", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("status_code", Integer, nullable=True),
    Column("response", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("actor_sub", Text),
    Column("actor_username", Text),
    Column("actor_display", Text),
    Column("client_id", Text),
    Column("via", Text),
    Column("approval_ref", Text),
    Column("approved_by", Text),
    Column("initiated_by", Text),
    Column("change_set_code", Text),
    Column("entity_type", Text, nullable=False),
    Column("entity_ref", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("before", JSONB),
    Column("after", JSONB),
    Column("trace_id", Text),
    Column("co_approved_by", ARRAY(Text)),  # M5 (0003)
)

outbox = Table(
    "outbox",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("event_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("seq", BigInteger, nullable=True),
    Column("changes", JSONB, nullable=True),
)

sim_clock = Table(
    "sim_clock",
    metadata,
    Column("singleton", Boolean, primary_key=True, server_default="true"),
    Column("today", Date, nullable=False),
    *_versioned(),
)

ui_session = Table(
    "ui_session",
    metadata,
    Column("id", Text, primary_key=True),
    Column("sub", Text, nullable=False),
    Column("username", Text, nullable=False),
    Column("name", Text),
    Column("email", Text),
    Column("roles", ARRAY(Text), nullable=False),
    Column("id_token", Text),
    Column("demo", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

# ───────────────────────── M2 (migrations/0002_m2_domain.sql) ─────────────────────────
port = Table(
    "port",
    metadata,
    Column("unlocode", Text, primary_key=True),
    Column("id", Uuid, server_default="uuidv7()"),
    Column("name", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("lat", Float, nullable=False),
    Column("lon", Float, nullable=False),
    Column("kind", Text, nullable=False),
    *_versioned(),
)

carrier = Table(
    "carrier",
    metadata,
    Column("code", Text, primary_key=True),
    Column("id", Uuid, server_default="uuidv7()"),
    Column("name", Text, nullable=False),
    Column("mode", Text, nullable=False),
    *_versioned(),
)

lane = Table(
    "lane",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("origin", Text, nullable=False),
    Column("dest", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("carrier_code", Text, nullable=False),
    Column("transit_days", Integer, nullable=False),
    Column("via", ARRAY(Text), nullable=False),
    Column("alt_lane_code", Text, nullable=True),
    *_versioned(),
)

rate_card = Table(
    "rate_card",
    metadata,
    Column("id", Uuid, server_default="uuidv7()"),
    Column("lane_code", Text, primary_key=True),
    Column("valid", DATERANGE, primary_key=True),
    Column("basis", Text, nullable=False),
    Column("rate_cents", BigInteger, nullable=False),
    Column("currency", Text, nullable=False, server_default="EUR"),
    Column("weekly_capacity", Integer, nullable=False),
    *_versioned(),
)

sailing = Table(
    "sailing",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("lane_code", Text, nullable=False),
    Column("vessel", Text, nullable=False),
    Column("cutoff", Date, nullable=False),
    Column("etd", Date, nullable=False),
    Column("eta", Date, nullable=False),
    Column("capacity_teu", Integer, nullable=False),
    Column("status", Text, nullable=False),
    *_versioned(),
)

capacity_booking = Table(
    "capacity_booking",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("lane_code", Text, nullable=False),
    Column("iso_week", Text, nullable=False),
    Column("qty_units", Integer, nullable=False),
    Column("basis", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("created_via", Text, nullable=False, server_default="ui"),
    *_versioned(),
)

shipment = Table(
    "shipment",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("po_code", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("lane_code", Text, nullable=True),
    Column("sailing_code", Text, nullable=True),
    Column("booking_code", Text, nullable=True),
    Column("origin", Text, nullable=False),
    Column("dest_dc_code", Text, nullable=False),
    Column("etd", Date, nullable=False),
    Column("eta", Date, nullable=False),
    Column("ata", Date, nullable=True),
    Column("status", Text, nullable=False),
    Column("created_via", Text, nullable=False, server_default="ui"),
    *_versioned(),
)

shipment_line = Table(
    "shipment_line",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("shipment_code", Text, nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("po_line_ref", Text, nullable=False),
    Column("sku_code", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    *_versioned(),
)

factory_line = Table(
    "factory_line",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("factory_code", Text, nullable=False),
    Column("capabilities", ARRAY(Text), nullable=False),
    Column("std_minutes_day", Integer, nullable=False),
    *_versioned(),
)

line_calendar = Table(
    "line_calendar",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("line_code", Text, nullable=False),
    Column("day", Date, nullable=False),
    Column("available_minutes", Integer, nullable=False),
    Column("status", Text, nullable=False),
    *_versioned(),
)

production_order = Table(
    "production_order",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("po_line_ref", Text, nullable=False),
    Column("line_code", Text, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("planned_start", Date, nullable=False),
    Column("planned_end", Date, nullable=False),
    Column("status", Text, nullable=False),
    Column("hold_reason", Text, nullable=True),
    *_versioned(),
)

online_order = Table(
    "online_order",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("market", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("store_code", Text, nullable=True),
    Column("promise_date", Date, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    *_versioned(),
)

online_order_line = Table(
    "online_order_line",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("order_code", Text, nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("sku_code", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("reserved_qty", Integer, nullable=False, server_default="0"),
    *_versioned(),
)

promotion = Table(
    "promotion",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("markets", ARRAY(Text), nullable=False),
    Column("grades", ARRAY(Text), nullable=False),
    Column("style_codes", ARRAY(Text), nullable=False),
    Column("period", DATERANGE, nullable=False),
    Column("discount_bp", Integer, nullable=False),
    Column("uplift_bp", Integer, nullable=False),
    *_versioned(),
)

sales_history = Table(
    "sales_history",
    metadata,
    Column("style_code", Text, primary_key=True),
    Column("store_code", Text, primary_key=True),
    Column("iso_week", Text, primary_key=True),
    Column("units", Integer, nullable=False),
)

api_key = Table(
    "api_key",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("name", Text, nullable=False),
    Column("prefix", Text, nullable=False, unique=True),
    Column("sha256", Text, nullable=False, unique=True),
    Column("scopes", ARRAY(Text), nullable=False),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    *_versioned(),
)

webhook_subscription = Table(
    "webhook_subscription",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("name", Text, nullable=False, unique=True),
    Column("url", Text, nullable=False),
    Column("events", ARRAY(Text), nullable=False),
    Column("secret_enc", Text, nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("created_by", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    *_versioned(),
)

webhook_delivery = Table(
    "webhook_delivery",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("subscription_id", Uuid, nullable=False),
    Column("event_id", Uuid, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("attempt", Integer, nullable=False, server_default="0"),
    Column("status", Text, nullable=False),
    Column("response_code", Integer, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
)

scn_snapshot = Table(
    "scn_snapshot",
    metadata,
    Column("id", Uuid, primary_key=True, server_default="uuidv7()"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("watermark", Text, nullable=False),
    Column("wal_lsn", Text, nullable=True),
    Column("manifest", JSONB, nullable=False),
    Column("created_by", Text, nullable=True),
)

oauth_client_seen = Table(
    "oauth_client_seen",
    metadata,
    Column("client_id", Text, primary_key=True),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default="now()"),
    Column("last_sub", Text, nullable=True),
    Column("last_username", Text, nullable=True),
    Column("surface", Text, nullable=True),
)
