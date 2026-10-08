/** Status → badge tone. Tones map to CSS classes `mx-badge--<tone>`. */

export type Tone = "neutral" | "info" | "progress" | "success" | "warning" | "danger" | "external";

const TONES: Record<string, Tone> = {
  // change sets
  draft: "neutral",
  validated: "info",
  applying: "progress",
  applied: "success",
  conflict: "warning",
  failed: "danger",
  rolled_back: "neutral",
  invalid: "danger",
  // change-set items
  pending: "neutral",
  ok: "info",
  error: "danger",
  // purchase orders / lines / transfer orders / allocations
  open: "info",
  in_production: "progress",
  shipped: "progress",
  received: "success",
  cancelled: "neutral",
  planned: "info",
  released: "progress",
  // supplier messages
  new: "external",
  read: "neutral",
  // M2 logistics: sailings, shipments, capacity bookings
  scheduled: "info",
  departed: "progress",
  arrived: "success",
  booked: "info",
  in_transit: "progress",
  held: "warning",
  confirmed: "success",
  // production orders / line calendar
  running: "progress",
  done: "success",
  closed: "neutral",
  reduced: "warning",
  // online orders
  picking: "progress",
  delivered: "success",
  // API keys, webhook subscriptions / deliveries
  active: "success",
  paused: "neutral",
  revoked: "neutral",
  retrying: "warning",
};

export function statusTone(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  return TONES[status] ?? "neutral";
}
