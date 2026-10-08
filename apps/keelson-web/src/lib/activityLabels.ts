/**
 * In-progress activity sentences for the thinking timeline.
 *
 * The engine labels a tool call that has started mechanically — "Checking list purchase orders
 * in Merxa" (the tool name with spaces, `keelson_engine.runtime.translator`) — and only the result
 * carries a humanised sentence ("Read 12 purchase orders from Merxa"), which replaces it. While a
 * call runs, that mechanical label is all the user sees, so it is rewritten here from the known
 * connector read tools (`20-merxa.md`). Any other label the engine sends is shown unchanged.
 */
const RUNNING: Record<string, (vendor: string) => string> = {
  list_purchase_orders: (v) => `Reading purchase orders from ${v}`,
  get_purchase_order: (v) => `Reading a purchase order from ${v}`,
  get_inventory: (v) => `Reading inventory positions from ${v}`,
  get_demand_forecast: (v) => `Reading the demand forecast from ${v}`,
  list_allocations: (v) => `Reading store allocations from ${v}`,
  list_locations: (v) => `Reading DCs and store clusters from ${v}`,
  list_stores: (v) => `Reading stores from ${v}`,
  list_transfer_orders: (v) => `Reading transfer orders from ${v}`,
  get_bom_where_used: (v) => `Checking where the components are used in ${v}`,
  get_entity: (v) => `Looking up a record in ${v}`,
  search_entities: (v) => `Searching ${v}`,
  get_change_set: (v) => `Reading the change batch in ${v}`,
  get_audit_trail: (v) => `Reading the audit trail in ${v}`,
  get_operator_guide: (v) => `Reading the operator guide in ${v}`,
  list_supplier_messages: (v) => `Reading supplier messages in ${v}`,
  // Merxa M2 read tools (`22-merxa-m2.md` §3) and the plain `/mcp-lite` reads.
  list_sailings: (v) => `Reading sailings from ${v}`,
  list_shipments: (v) => `Reading shipments from ${v}`,
  get_lane_options: (v) => `Reading lane options from ${v}`,
  get_factory_schedule: (v) => `Reading the factory schedule from ${v}`,
  list_online_orders_at_risk: (v) => `Reading online orders at risk from ${v}`,
  list_promotions: (v) => `Reading promotions from ${v}`,
  list_events: (v) => `Reading recent events from ${v}`,
  list_items: (v) => `Reading records from ${v}`,
  get_item: (v) => `Reading a record from ${v}`,
  calc: () => "Calculating over the evidence",
};

const MECHANICAL = /^Checking (.+) in (.+)$/;

/** The label to show for a tool call that has started (`TOOL_CALL_START`). */
export function runningActivityLabel(toolName: string | null | undefined, label: string | null | undefined): string {
  const name = toolName ?? "";
  const match = label ? MECHANICAL.exec(label) : null;
  const mechanical = !!match && !!name && match[1]?.toLowerCase() === name.replace(/_/g, " ").toLowerCase();
  if (label && !mechanical) return label;
  const vendor = match?.[2] ?? "your systems";
  // Own keys only: tool names come from third-party MCP servers ("constructor", "toString", …).
  const phrase = Object.hasOwn(RUNNING, name) ? RUNNING[name] : undefined;
  if (phrase) return phrase(vendor);
  return match ? `Looking things up in ${vendor}` : "Checking your systems";
}
