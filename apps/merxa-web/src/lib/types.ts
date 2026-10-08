/**
 * Response shapes of merxa-api `/v1/*` (services/merxa-api/src/merxa_api/services/*.py).
 * Dates are ISO `YYYY-MM-DD`, timestamps RFC 3339 UTC, quantities integer eaches,
 * money integer cents.
 */

export type Page<T> = { items: T[]; next_cursor: string | null };

export type Persona = { username: string; name: string; role_label: string; initials: string };

export type SessionUser = {
  sub: string;
  username: string;
  name: string | null;
  email: string | null;
  roles: string[];
  scopes: string[];
};

export type Me = { user: SessionUser; demo_mode: boolean; brand: { vendor: string } };

export type Dc = {
  code: string;
  name: string;
  country: string;
  city: string;
  lat: number;
  lon: number;
  inbound_cap_units_wk: number;
  outbound_cap_units_wk: number;
  version: number;
};

export type Cluster = {
  code: string;
  dc_code: string;
  climate: "cold" | "mild" | "warm";
  grade_band: "A" | "BC";
  store_count: number;
  version: number;
};

export type Store = {
  code: string;
  name: string;
  market: string;
  city: string;
  lat: number;
  lon: number;
  grade: "A" | "B" | "C";
  cluster: string;
  home_dc: string;
  sales_index: number;
  version: number;
};

export type Supplier = { code: string; name: string; country: string; kind: string; version: number };
export type Factory = { code: string; name: string; country: string; supplier_code: string; version: number };
export type Style = {
  code: string;
  name: string;
  family: string;
  season: string;
  colour: string;
  retail_price_cents: number;
  unit_cost_cents: number;
  version: number;
};

export type PoStatus = "open" | "in_production" | "shipped" | "received" | "cancelled";

export type PurchaseOrderSummary = {
  code: string;
  supplier_code: string;
  factory_code: string;
  dest_dc: string;
  status: PoStatus;
  ex_factory_date: string | null;
  eta_dc: string | null;
  line_count: number;
  total_qty: number;
  version: number;
};

export type PoLine = {
  line_no: number;
  sku: string;
  qty_ordered: number;
  qty_received: number;
  eta_dc: string | null;
  status: string;
  version: number;
};

export type PurchaseOrder = PurchaseOrderSummary & { lines: PoLine[] };

export type InventoryPosition = {
  location_type: "dc" | "store";
  location_code: string;
  sku: string;
  on_hand: number;
  allocated: number;
  reserved: number;
  in_transit: number;
  atp: number;
  version: number;
};

export type TransferOrderStatus = "planned" | "released" | "shipped" | "received" | "cancelled";

export type TransferOrder = {
  code: string;
  from_dc: string;
  to_dc: string;
  ship_date: string;
  eta_date: string;
  status: TransferOrderStatus;
  created_via: string;
  created_at: string;
  version: number;
  lines: { line_no: number; sku: string; qty: number }[];
  total_qty: number;
};

export type SupplierMessage = {
  id: string;
  supplier_code: string;
  received_at: string;
  subject: string;
  /** Free text from an external party: untrusted, always rendered as plain text. */
  body: string;
  status: "new" | "read";
  version: number;
};

export type ChangeSetStatus =
  | "draft"
  | "validated"
  | "applying"
  | "applied"
  | "conflict"
  | "failed"
  | "rolled_back";

/** Row of `GET /v1/change-sets`. */
export type ChangeSetSummary = {
  change_set_id: string;
  code: string;
  title: string;
  status: ChangeSetStatus;
  items_count: number;
  preview_hash: string | null;
  approval_ref: string | null;
  approved_by: string | null;
  initiated_by: string | null;
  applied_by_display: string | null;
  applied_by_username: string | null;
  via: string | null;
  created_at: string;
  validated_at: string | null;
  applied_at: string | null;
  version: number;
  attribution_text: string | null;
  /** M5: other members of a four-eyes quorum, from the approval receipt (null = none). */
  co_approvers?: { ref: string; name: string | null }[] | null;
  rollback_of?: string | null;
};

export type ChangeSetItemStatus = "pending" | "ok" | "error" | "applied";

export type ChangeSetItem = {
  seq: number;
  op: string;
  target_ref: string | null;
  expected_version: number | null;
  payload: Record<string, unknown>;
  before: unknown;
  after: unknown;
  status: ChangeSetItemStatus;
  error: string | null;
};

export type Conflict = {
  seq: number;
  target_ref: string | null;
  expected_version: number | null;
  actual_version: number | null;
  reason: string;
};

export type ChangeSetPreview = {
  status?: string;
  preview_hash?: string;
  totals?: { lines: number; units_moved: number; value_cents: number };
};

/** `GET /v1/change-sets/{id|code}`. */
export type ChangeSet = Omit<ChangeSetSummary, "applied_by_display" | "applied_by_username"> & {
  origin: { system?: string | null; case_ref?: string | null; plan_version?: number | string | null } & Record<
    string,
    unknown
  >;
  preview: ChangeSetPreview | null;
  result: { status?: string; conflicts?: Conflict[]; applied?: number } | null;
  receipt_jti: string | null;
  applied_by_sub: string | null;
  applied_by_username: string | null;
  applied_by_display: string | null;
  applied_via_client: string | null;
  created_by_client: string | null;
  created_by_sub: string | null;
  rollback_of: string | null;
  items: ChangeSetItem[];
  applied_by: { sub: string | null; username: string | null; display: string | null; client_id: string | null };
  links: { ui: string };
};

export type AuditRow = {
  id: number;
  at: string;
  actor_sub: string | null;
  actor_username: string | null;
  actor_display: string | null;
  client_id: string | null;
  via: string | null;
  approval_ref: string | null;
  approved_by: string | null;
  initiated_by: string | null;
  change_set_code: string | null;
  entity_type: string;
  entity_ref: string;
  action: string;
  before: unknown;
  after: unknown;
  trace_id: string | null;
};

export type OperatorGuide = {
  change_set_id: string;
  code: string;
  locale: string;
  steps: {
    seq: number;
    item_seq: number;
    /** M5: the change-set op and target of the step, and what a read-back finds once it is done. */
    op?: string;
    target_ref?: string | null;
    title: string;
    instructions: string;
    deep_link: string;
    expected?: { entity_type: string; entity_ref: string | null; match: Record<string, unknown> } | null;
  }[];
};

/** RFC 9457 problem document as rendered by merxa-api. */
export type ProblemDocument = {
  type?: string;
  title?: string;
  status?: number;
  code?: string;
  detail?: string;
  [extension: string]: unknown;
};

// ───────────────────────── M2 (docs/contracts/22-merxa-m2.md) ─────────────────────────
// Shapes of services/merxa-api/src/merxa_api/services/{supply,apikeys,clients,webhooks}.py.

export type LaneMode = "sea" | "air" | "sea_air" | "road";
export type RateBasis = "per_unit" | "per_kg" | "per_teu";

export type Lane = {
  code: string;
  origin: string;
  dest: string;
  mode: LaneMode;
  carrier_code: string;
  carrier_name: string;
  transit_days: number;
  via: string[];
  alt_lane_code: string | null;
  version: number;
};

export type RateCard = {
  lane_code: string;
  /** Inclusive validity; null = unbounded side of the `daterange`. */
  valid_from: string | null;
  valid_to: string | null;
  basis: RateBasis;
  rate_cents: number;
  currency: string;
  weekly_capacity: number;
  version: number;
};

export type SailingStatus = "scheduled" | "departed" | "arrived" | "cancelled";

export type Sailing = {
  code: string;
  lane_code: string;
  origin: string;
  dest: string;
  vessel: string;
  cutoff: string;
  etd: string;
  eta: string;
  capacity_teu: number;
  status: SailingStatus;
  version: number;
};

export type CapacityBookingStatus = "held" | "confirmed" | "released";

export type CapacityBooking = {
  code: string;
  lane_code: string;
  iso_week: string;
  qty_units: number;
  basis: RateBasis;
  status: CapacityBookingStatus;
  expires_at: string | null;
  created_via: string;
  version: number;
};

export type ShipmentStatus = "planned" | "booked" | "in_transit" | "arrived" | "cancelled";

export type ShipmentLine = { line_no: number; po_line_ref: string; sku: string; qty: number };

export type Shipment = {
  code: string;
  po_code: string;
  mode: LaneMode;
  lane_code: string | null;
  sailing_code: string | null;
  booking_code: string | null;
  origin: string;
  dest_dc_code: string;
  dest_dc: string;
  etd: string;
  eta: string;
  ata: string | null;
  status: ShipmentStatus;
  created_via: string;
  version: number;
  lines: ShipmentLine[];
  total_qty: number;
};

/** `GET /v1/lane-options` (only `week` is used by the UI: the simulation's current ISO week). */
export type LaneOptions = { origin: string; dest: string; week: string; options: unknown[] };

export type FactoryLine = {
  code: string;
  factory_code: string;
  capabilities: string[];
  std_minutes_day: number;
  version: number;
};

export type ProductionOrderStatus = "planned" | "held" | "running" | "done";

export type ProductionOrder = {
  code: string;
  /** `<po code>#<line no>` */
  po_line_ref: string;
  line_code: string;
  seq: number;
  planned_start: string;
  planned_end: string;
  status: ProductionOrderStatus;
  hold_reason: string | null;
  version: number;
};

export type CalendarDayStatus = "open" | "closed" | "reduced";

export type CalendarDay = { day: string; available_minutes: number; status: CalendarDayStatus };

/** `GET /v1/factories/{code}/schedule` */
export type FactorySchedule = {
  factory_code: string;
  from: string;
  to: string;
  lines: (Omit<FactoryLine, "factory_code"> & { calendar: CalendarDay[]; orders: ProductionOrder[] })[];
};

export type OnlineOrderStatus = "open" | "picking" | "shipped" | "delivered" | "cancelled";

export type OnlineOrder = {
  code: string;
  market: string;
  channel: "online" | "click_collect";
  store_code: string | null;
  promise_date: string;
  status: OnlineOrderStatus;
  created_at: string;
  version: number;
  lines: { line_no: number; sku: string; qty: number; reserved_qty: number; version: number }[];
  /** Units ordered but not reserved. */
  shortfall: number;
  at_risk: boolean;
};

export type Promotion = {
  code: string;
  name: string;
  markets: string[];
  grades: string[];
  style_codes: string[];
  /** Inclusive period; null = unbounded side of the `daterange` (the schema allows it). */
  period_start: string | null;
  period_end: string | null;
  /** Basis points: 2500 = 25 %. */
  discount_bp: number;
  uplift_bp: number;
  version: number;
};

export type ApiKey = {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_by: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  version: number;
  status: "active" | "revoked";
};

/** `POST /v1/api-keys` (201). `key` is present only in the first response (null on replay). */
export type MintedApiKey = {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_by: string;
  created_at: string;
  key: string | null;
  key_shown_once: boolean;
};

export type OAuthClientSeen = {
  client_id: string;
  last_seen_at: string;
  last_sub: string | null;
  last_username: string | null;
  /** "mcp" | "mcp-lite" | "rest" */
  surface: string | null;
};

export type WebhookSubscription = {
  id: string;
  name: string;
  url: string;
  events: string[];
  active: boolean;
  created_by: string | null;
  created_at: string;
  version: number;
  delivered: number;
  pending: number;
  failed: number;
  last_delivered_at: string | null;
};

/** `POST /v1/webhook-subscriptions` (201). `secret` only in the first response (null on replay). */
export type CreatedWebhookSubscription = {
  id: string;
  name: string;
  url: string;
  events: string[];
  active: boolean;
  created_at: string;
  version: number;
  secret: string | null;
  secret_shown_once: boolean;
};

export type WebhookDeliveryStatus = "pending" | "delivered" | "failed";

export type WebhookDelivery = {
  id: string;
  subscription_id: string;
  event_id: string;
  event_type: string;
  event_seq: number | null;
  /** Attempts made so far (0 = not yet attempted). */
  attempt: number;
  status: WebhookDeliveryStatus;
  response_code: number | null;
  last_error: string | null;
  next_attempt_at: string | null;
  created_at: string;
  delivered_at: string | null;
};

// ───────────────────────── M5 (docs/contracts/95 §2): manual changes ─────────────────────────
// Shapes of services/merxa-api/src/merxa_api/services/manual.py and api/routes_m5.py.

export type Allocation = {
  code: string;
  sku: string;
  dc_code: string;
  store_code: string;
  ship_week: string;
  qty: number;
  status: string;
  version: number;
};

/** Common fields of a manual-change response (= the webhook event's data). */
export type ManualChange = {
  event: string;
  entity_type: string;
  entity_ref: string;
  source: "manual";
  via: string;
  changed_by: { username: string | null; display: string };
  version: number;
};

export type TransferOrderCreated = ManualChange & {
  code: string;
  from_dc: string;
  to_dc: string;
  ship_date: string;
  eta_date: string;
  status: string;
  lines: { line_no: number; sku: string; qty: number }[];
  units: number;
};

export type AllocationUpdated = ManualChange & { code: string; qty: number; qty_before: number };

export type PoLineUpdated = ManualChange & {
  ref: string;
  action: "reschedule" | "change_qty";
  eta_dc: string;
  qty_ordered: number;
};

export type BookingChanged = ManualChange & {
  code: string;
  lane_code: string;
  iso_week: string;
  qty_units: number;
  status: CapacityBookingStatus;
};

export type CapacityBookingDetail = CapacityBooking & {
  shipments: { code: string; po_code: string; mode: LaneMode; status: ShipmentStatus; etd: string; eta: string }[];
  lane_week: { remaining_capacity: number | null; weekly_capacity: number | null; basis: RateBasis | null };
  today: string;
};
