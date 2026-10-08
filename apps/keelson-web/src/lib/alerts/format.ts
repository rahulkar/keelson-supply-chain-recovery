/**
 * Pure presentation helpers for the Alerts inbox (M6, `docs/contracts/100` §8). Every number comes
 * from the alert's deterministic `exposure_summary` / exposures; nothing here invents values.
 */
import { exposedSites, type BlockEnvelope, type NetworkMapData, type NetworkMapProps, type SignalsExposureSummary } from "@keelson/contracts";
import { humanize } from "@keelson/viz";
import type { Alert, AlertDetailView, AlertExposure, AlertSignal } from "./types";

const count = new Intl.NumberFormat("en-GB");

function plural(n: number, one: string, many: string): string {
  return `${count.format(n)} ${n === 1 ? one : many}`;
}

/** "3 sites · 4 lanes · 2 open PO lines" (zero parts left out; "No exposed sites" when nothing). */
export function exposureSummaryText(summary: SignalsExposureSummary): string {
  const parts: string[] = [];
  const sites = exposedSites(summary);
  if (sites > 0) parts.push(plural(sites, "site", "sites"));
  if (summary.nodes_by_type.lane > 0) parts.push(plural(summary.nodes_by_type.lane, "lane", "lanes"));
  if (summary.open_po_lines !== null && summary.open_po_lines > 0) parts.push(plural(summary.open_po_lines, "open PO line", "open PO lines"));
  return parts.length ? parts.join(" · ") : "No exposed sites";
}

const NODE_TYPE_LABELS: Record<string, [string, string]> = {
  supplier: ["supplier", "suppliers"],
  factory: ["factory", "factories"],
  port: ["port", "ports"],
  dc: ["distribution centre", "distribution centres"],
  store: ["store cluster", "store clusters"],
  lane: ["lane", "lanes"],
};

/** Detailed breakdown: "1 supplier, 1 factory, 1 port, 4 lanes". */
export function exposureBreakdown(summary: SignalsExposureSummary): string[] {
  const order = ["supplier", "factory", "port", "dc", "store", "lane"] as const;
  return order
    .filter((t) => summary.nodes_by_type[t] > 0)
    .map((t) => plural(summary.nodes_by_type[t], NODE_TYPE_LABELS[t]![0], NODE_TYPE_LABELS[t]![1]));
}

export function nodeTypeLabel(type: string, n = 1): string {
  const labels = NODE_TYPE_LABELS[type];
  if (!labels) return humanize(type);
  return n === 1 ? labels[0] : labels[1];
}

/** How a node was matched to the event, in words. */
export function exposureMethodText(e: Pick<AlertExposure, "method" | "distance_km">): string {
  switch (e.method) {
    case "intersects":
      return "Inside the affected area";
    case "dwithin_km":
      return e.distance_km !== null ? `${count.format(e.distance_km)} km from the affected area` : "Near the affected area";
    case "lane_crosses":
      return "Route crosses the affected area";
    case "entity_match":
      return "Named in the report";
    default:
      return humanize(String(e.method));
  }
}

/** "just now", "12 min ago", "3 h ago", "2 days ago", then a date. Future times read "just now". */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  if (d < 7) return d === 1 ? "yesterday" : `${d} days ago`;
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).format(new Date(t));
}

/** Status in words (inbox and drawer). */
export const STATUS_LABELS: Record<Alert["status"], string> = {
  new: "New",
  seen: "Seen",
  snoozed: "Snoozed",
  dismissed: "Dismissed",
  opened: "Case opened",
  resolved: "Resolved",
};

/** Licence in parentheses unless it already is the source name: "GDACS (CC BY 4.0)". */
export function attribution(source: Pick<AlertSignal["source"], "name" | "license">): string {
  const name = source.name.trim();
  const licence = source.license.trim();
  return licence && licence.toLowerCase() !== name.toLowerCase() ? `${name} (${licence})` : name;
}

/** Hosts a public source never has: single labels (service names), localhost, IP literals. */
function isInternalHost(hostname: string): boolean {
  const h = hostname.toLowerCase().replace(/\.$/, "");
  if (!h.includes(".") || h === "localhost" || h.endsWith(".localhost") || h.endsWith(".internal") || h.endsWith(".local")) return true;
  if (h.startsWith("[")) return true; // IPv6 literal
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(h);
}

/**
 * A source link only for plain http(s) URLs on a public-looking host (the contract pattern
 * already enforces the scheme; checked again). Feed / news text is untrusted: a link to an
 * internal service name, localhost or an IP literal is shown as text, never as a link.
 */
export function safeSourceUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    if ((u.protocol !== "http:" && u.protocol !== "https:") || u.username || u.password) return null;
    if (isInternalHost(u.hostname)) return null;
    return u.toString();
  } catch {
    return null;
  }
}

export type SourceView = {
  key: string;
  name: string;
  attribution: string;
  url: string | null;
  provenance: AlertSignal["provenance"];
  retrievedAt: string;
  signals: number;
};

/** Distinct sources of the event's member signals (newest retrieval first). */
export function alertSources(signals: readonly AlertSignal[]): SourceView[] {
  const byKey = new Map<string, SourceView>();
  for (const s of signals) {
    const key = `${s.source.connector_id}|${s.source.name}`;
    const prev = byKey.get(key);
    const url = safeSourceUrl(s.source.url);
    if (!prev) {
      byKey.set(key, {
        key,
        name: s.source.name,
        attribution: attribution(s.source),
        url,
        provenance: s.provenance,
        retrievedAt: s.source.retrieved_at,
        signals: 1,
      });
      continue;
    }
    prev.signals += 1;
    if (Date.parse(s.source.retrieved_at) > Date.parse(prev.retrievedAt)) {
      prev.retrievedAt = s.source.retrieved_at;
      if (url) prev.url = url;
    }
    // A source is "fact" only when every one of its signals is.
    if (prev.provenance === "fact" && s.provenance !== "fact") prev.provenance = s.provenance;
  }
  return [...byKey.values()].sort((a, b) => Date.parse(b.retrievedAt) - Date.parse(a.retrievedAt));
}

export type TimelineEntry = { key: string; at: string; text: string; source: string | null; provenance: AlertSignal["provenance"] | null };

/**
 * Updates of the event, newest first: each member signal (title, source, version) and the alert's
 * own milestones (raised, last updated).
 */
export function alertTimeline(detail: Pick<AlertDetailView, "alert" | "signals">): TimelineEntry[] {
  const entries: TimelineEntry[] = detail.signals.map((s) => ({
    key: `sig:${s.id}`,
    at: s.observed_at,
    text: s.version > 1 ? `${s.title} (update ${s.version})` : s.title,
    source: s.source.name,
    provenance: s.provenance,
  }));
  entries.push({ key: "alert:raised", at: detail.alert.first_raised_at, text: "Alert raised", source: null, provenance: null });
  if (detail.alert.updated_at !== detail.alert.first_raised_at) {
    entries.push({ key: "alert:updated", at: detail.alert.updated_at, text: `Alert updated (${STATUS_LABELS[detail.alert.status].toLowerCase()})`, source: null, provenance: null });
  }
  // Same instant: the source update before the alert milestone it caused.
  const rank = (e: TimelineEntry) => (e.source !== null ? 0 : 1);
  return entries.sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || rank(a) - rank(b) || a.key.localeCompare(b.key));
}

// ─────────────────────────────── map ───────────────────────────────

type Position = [number, number];
type Ring = Position[];
type Geometry = NonNullable<AlertDetailView["event"]["geometry"]>;

const E6 = 1_000_000;
const clampE6 = (v: number, lim: number) => Math.max(-lim * E6, Math.min(lim * E6, Math.round(v * E6)));
const toE6 = ([lon, lat]: Position): [number, number] => [clampE6(lon, 180), clampE6(lat, 90)];

/** A small hexagon (~radiusKm) around a point, so point / track events still show an area. */
function hexagon([lon, lat]: Position, radiusKm: number): Ring {
  const dLat = radiusKm / 111;
  const dLon = radiusKm / (111 * Math.max(0.1, Math.cos((lat * Math.PI) / 180)));
  const ring: Ring = [];
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i;
    ring.push([Math.max(-180, Math.min(180, lon + dLon * Math.cos(a))), Math.max(-90, Math.min(90, lat + dLat * Math.sin(a)))]);
  }
  ring.push(ring[0]!);
  return ring;
}

const POINT_RADIUS_KM = 25;
const MAX_TRACK_POINTS = 200;

/** Polygons that outline a geometry (polygons as given; points and line vertices as small hexagons). */
export function footprintPolygons(geometry: Geometry): Ring[][] {
  switch (geometry.type) {
    case "Polygon":
      return [geometry.coordinates as Ring[]];
    case "MultiPolygon":
      return geometry.coordinates as Ring[][];
    case "Point":
      return [[hexagon(geometry.coordinates as Position, POINT_RADIUS_KM)]];
    case "MultiPoint":
    case "LineString":
      return (geometry.coordinates as Position[]).slice(0, MAX_TRACK_POINTS).map((p) => [hexagon(p, POINT_RADIUS_KM)]);
    case "MultiLineString":
      return (geometry.coordinates as Position[][]).flat().slice(0, MAX_TRACK_POINTS).map((p) => [hexagon(p, POINT_RADIUS_KM)]);
    case "GeometryCollection":
      return geometry.geometries.flatMap((g) => footprintPolygons(g as Geometry));
    default:
      return [];
  }
}

function bboxOf(points: [number, number][]): NetworkMapProps["bbox"] {
  let [minX, minY, maxX, maxY] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [x, y] of points) {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  }
  // Padding: 20 % of the extent, at least 2 degrees, so a small footprint keeps some context.
  const padX = Math.max((maxX - minX) * 0.2, 2 * E6);
  const padY = Math.max((maxY - minY) * 0.2, 2 * E6);
  return [
    Math.max(-180 * E6, Math.round(minX - padX)),
    Math.max(-90 * E6, Math.round(minY - padY)),
    Math.min(180 * E6, Math.round(maxX + padX)),
    Math.min(90 * E6, Math.round(maxY + padY)),
  ];
}

/**
 * The drawer's map as a `network_map@1` block (drawn by the existing NetworkMap visual): the
 * engine's ready map when it sent one, else the event footprint alone (exposed nodes are then
 * listed below the map, the hub's exposures carry no coordinates). `null` without any geometry.
 */
export function alertMapBlock(detail: AlertDetailView): { envelope: BlockEnvelope; data: NetworkMapData } | null {
  const { alert, event } = detail;
  const asOf = (alert.updated_at.match(/^\d{4}-\d{2}-\d{2}/)?.[0] ?? "1970-01-01") as string;
  const base = {
    id: `blk_alert_map_${alert.id}`,
    kind: "network_map" as const,
    v: 1 as const,
    title: "Where it is",
    placement: "canvas" as const,
    provenance: { kind: alert.simulated ? ("simulated" as const) : ("fact" as const), engine: "signal-hub", inputs_hash: null, as_of: asOf },
    data_ref: null,
  };
  if (detail.map) {
    return { envelope: { ...base, props: { ...detail.map.props, title: "Where it is" } } as BlockEnvelope, data: detail.map.data };
  }
  if (!event.geometry) return null;
  const polygons = footprintPolygons(event.geometry)
    .map((poly) => poly.map((ring) => ring.map(toE6)))
    .filter((poly) => poly.length > 0 && poly[0]!.length > 0);
  if (polygons.length === 0) return null;
  const label = event.title;
  const data: NetworkMapData = {
    nodes: [],
    links: [],
    footprint: {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: { label, status: "disrupted" },
          geometry: polygons.length === 1 ? { type: "Polygon", coordinates: polygons[0]! } : { type: "MultiPolygon", coordinates: polygons },
        },
      ],
    },
  };
  const props: NetworkMapProps = {
    title: "Where it is",
    summary: { nodes: 0, at_risk_nodes: 0, lanes: 0 },
    bbox: bboxOf(polygons.flat(2)),
    legend: [{ key: "disrupted", label: "Affected area", kind: "footprint" }],
  };
  return { envelope: { ...base, props } as BlockEnvelope, data };
}
