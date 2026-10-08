import { hexColor, rgba, type ColorRole, type ThemeName } from "@keelson/tokens";
import type { BadgeTone, BadgeVariant } from "@keelson/ui";

/** Status of a node / lane / footprint in the M3 visuals (contracts `NodeStatus`). */
export type NodeStatus = "disrupted" | "impacted" | "at_risk" | "ok";

/** Most severe first (sorting, legends, filter chips). */
export const NODE_STATUSES: readonly NodeStatus[] = ["disrupted", "impacted", "at_risk", "ok"];

export const statusMeta: Record<
  NodeStatus,
  { label: string; tone: BadgeTone; variant: BadgeVariant; color: ColorRole; rank: number; description: string }
> = {
  disrupted: { label: "Disrupted", tone: "danger", variant: "soft", color: "sev-critical", rank: 0, description: "Directly hit by the disruption" },
  impacted: { label: "Impacted", tone: "warning", variant: "soft", color: "sev-high", rank: 1, description: "Downstream of a disrupted node" },
  at_risk: { label: "At risk", tone: "warning", variant: "outline", color: "sev-medium", rank: 2, description: "May run short if nothing changes" },
  ok: { label: "OK", tone: "neutral", variant: "soft", color: "sev-low", rank: 3, description: "Not affected" },
};

export function statusLabel(status: NodeStatus): string {
  return statusMeta[status].label;
}

/** Hex colour for a status (ECharts, xyflow inline styles). */
export function statusHex(status: NodeStatus, theme: ThemeName): string {
  return hexColor(statusMeta[status].color, theme);
}

/** `[r, g, b, a]` for deck.gl. */
export function statusRgba(status: NodeStatus, theme: ThemeName, alpha = 255): [number, number, number, number] {
  const [r, g, b] = rgba(statusMeta[status].color, theme);
  return [r, g, b, alpha];
}

export function compareStatus(a: NodeStatus, b: NodeStatus): number {
  return statusMeta[a].rank - statusMeta[b].rank;
}

/** Stock cell status in the stockout timeline. */
export type CellStatus = "ok" | "tight" | "short";

export const cellMeta: Record<CellStatus, { label: string; color: ColorRole }> = {
  ok: { label: "Enough stock", color: "surface-3" },
  tight: { label: "Tight", color: "sev-medium" },
  short: { label: "Short", color: "sev-critical" },
};

/** Turns an id like `opt_sea_air` into "Sea air" when no title is known. */
export function humanizeId(id: string): string {
  const words = id.replace(/^opt[_-]/i, "").replace(/[_-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id;
}
