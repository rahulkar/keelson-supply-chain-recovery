/**
 * Pure model of what the network map draws. The map canvas (deck.gl layers) and the
 * "View as table" tables are both built from {@link buildMapModel}, so they always show the
 * same sites and lanes for the selected plan option.
 */
import type { MapLink, MapNode, NetworkMapData, NetworkMapProps } from "@keelson/contracts";
import type { ColorRole } from "@keelson/tokens";
import { compareStatus, humanizeId, statusMeta, type NodeStatus } from "../lib/status";

export type LngLat = [number, number];

/** Above these sizes the payload is not drawn (table only): keeps the browser responsive. */
export const MAP_LIMITS = { nodes: 5_000, links: 5_000, pathPoints: 100_000 } as const;

/** `all` (current network + every option), `current` (no option links) or one option id. */
export type MapOptionFilter = string;
export const ALL_OPTIONS = "all";
export const CURRENT_ONLY = "current";

export type MapOptionChoice = { id: string; label: string; links: number };

export type MapModel = {
  nodes: MapNode[];
  /** Lanes drawn as paths (inbound, transfer). */
  paths: MapLink[];
  /** Lanes drawn as arcs (sea–air, near-shore). */
  arcs: MapLink[];
  /** Every visible lane, paths and arcs, in table order. */
  links: MapLink[];
  /** What the canvas draws: polyline legs and arc legs (a multi-leg sea–air lane has both). */
  legs: { paths: PathLeg[]; arcs: ArcLeg[] };
  footprint: FootprintGeoJson | null;
};

export type FootprintFeatureGeoJson = {
  type: "Feature";
  geometry: { type: "Polygon"; coordinates: LngLat[][] } | { type: "MultiPolygon"; coordinates: LngLat[][][] };
  properties: { label: string; status: NodeStatus };
};
export type FootprintGeoJson = { type: "FeatureCollection"; features: FootprintFeatureGeoJson[] };

export const nodeKindLabels: Record<MapNode["kind"], string> = {
  dc: "Distribution centre",
  cluster: "Store cluster",
  store: "Store",
  factory: "Factory",
  supplier: "Supplier",
  port: "Port",
  airport: "Airport",
  chokepoint: "Chokepoint",
};

export const linkKindLabels: Record<MapLink["kind"], string> = {
  inbound: "Inbound",
  transfer: "Transfer",
  sea_air: "Sea–air",
  nearshore: "Near-shore",
};

/** Arc-drawn lane kinds (proposed by expedite / near-shore levers). */
export const ARC_KINDS: ReadonlySet<MapLink["kind"]> = new Set(["sea_air", "nearshore"]);

export function e6ToDeg(v: number): number {
  return v / 1_000_000;
}

export function toLngLat([lon, lat]: readonly [number, number]): LngLat {
  return [e6ToDeg(lon), e6ToDeg(lat)];
}

export function nodePosition(n: Pick<MapNode, "lon_e6" | "lat_e6">): LngLat {
  return [e6ToDeg(n.lon_e6), e6ToDeg(n.lat_e6)];
}

/**
 * `[w, s, e, n]` (e6) → MapLibre bounds in degrees; a zero-size box is padded so it can be fitted.
 * A box that crosses the antimeridian (`w > e`, e.g. Los Angeles → Shanghai) keeps its eastern edge
 * past 180° (MapLibre's convention), and a south / north pair given the wrong way round is swapped.
 */
export function bboxToBounds(bbox: NetworkMapProps["bbox"]): [LngLat, LngLat] {
  let [w, s, e, n] = bbox.map(e6ToDeg) as [number, number, number, number];
  if (s > n) [s, n] = [n, s];
  if (w > e) e += 360;
  if (e - w < 0.5) {
    w -= 0.5;
    e += 0.5;
  }
  if (n - s < 0.5) {
    s -= 0.5;
    n += 0.5;
  }
  const clampLat = (v: number) => Math.max(-85, Math.min(85, v));
  w = Math.max(-180, w);
  e = Math.min(e > 180 ? w + 360 : 180, e);
  return [
    [w, clampLat(s)],
    [e, clampLat(n)],
  ];
}

export function mapSize(data: NetworkMapData): { nodes: number; links: number; pathPoints: number } {
  let pathPoints = 0;
  for (const l of data.links) pathPoints += l.path.length;
  for (const f of data.footprint?.features ?? []) {
    const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    for (const p of polys) for (const ring of p) pathPoints += ring.length;
  }
  return { nodes: data.nodes.length, links: data.links.length, pathPoints };
}

/** Too big to draw (the table still shows everything, paged). */
export function isOversized(data: NetworkMapData): boolean {
  const s = mapSize(data);
  return s.nodes > MAP_LIMITS.nodes || s.links > MAP_LIMITS.links || s.pathPoints > MAP_LIMITS.pathPoints;
}

/** Plan options that propose lanes, in order of first appearance; labels from the legend when it names them. */
export function mapOptions(data: NetworkMapData, legend: NetworkMapProps["legend"], titles?: Record<string, string>): MapOptionChoice[] {
  const counts = new Map<string, number>();
  for (const l of data.links) if (l.option_id) counts.set(l.option_id, (counts.get(l.option_id) ?? 0) + 1);
  const legendLabel = new Map(legend.filter((i) => i.kind === "link").map((i) => [i.key, i.label]));
  return [...counts.entries()].map(([id, links]) => ({ id, label: titles?.[id] ?? legendLabel.get(id) ?? humanizeId(id), links }));
}

export function linkVisible(link: MapLink, filter: MapOptionFilter): boolean {
  if (filter === ALL_OPTIONS) return true;
  if (link.option_id === null) return true;
  return filter !== CURRENT_ONLY && link.option_id === filter;
}

function footprintGeoJson(data: NetworkMapData): FootprintGeoJson | null {
  if (!data.footprint || data.footprint.features.length === 0) return null;
  return {
    type: "FeatureCollection",
    features: data.footprint.features.map((f) => ({
      type: "Feature",
      properties: { label: f.properties.label, status: f.properties.status },
      geometry:
        f.geometry.type === "Polygon"
          ? { type: "Polygon", coordinates: f.geometry.coordinates.map((ring) => ring.map(toLngLat)) }
          : { type: "MultiPolygon", coordinates: f.geometry.coordinates.map((poly) => poly.map((ring) => ring.map(toLngLat))) },
    })),
  };
}

const byStatusThenUnits = <T extends { status: NodeStatus; label?: string; id: string }>(units: (x: T) => number) => (a: T, b: T) =>
  compareStatus(a.status, b.status) || units(b) - units(a) || a.id.localeCompare(b.id);

/** What the map draws (and the tables list) for an option filter. Sites sorted by severity. */
export function buildMapModel(data: NetworkMapData, filter: MapOptionFilter): MapModel {
  const nodes = [...data.nodes].sort(byStatusThenUnits<MapNode>((n) => n.units_at_risk ?? 0));
  const links = data.links.filter((l) => linkVisible(l, filter)).sort(byStatusThenUnits<MapLink>((l) => l.units ?? 0));
  return {
    nodes,
    links,
    paths: links.filter((l) => !ARC_KINDS.has(l.kind)),
    arcs: links.filter((l) => ARC_KINDS.has(l.kind)),
    legs: laneLegs(links),
    footprint: footprintGeoJson(data),
  };
}

/** Pixel radius of a site marker (bigger for DCs / ports, a little for units at risk). */
export function nodeRadius(n: Pick<MapNode, "kind" | "units_at_risk">): number {
  const base: Record<MapNode["kind"], number> = {
    dc: 9,
    port: 7,
    airport: 7,
    chokepoint: 7,
    cluster: 6,
    factory: 6,
    supplier: 6,
    store: 4,
  };
  const units = n.units_at_risk ?? 0;
  return base[n.kind] + (units > 0 ? Math.min(5, Math.log10(units + 1)) : 0);
}

/** Lane colour role: a current lane shows its status when affected, otherwise its kind; proposed lanes show their kind. */
export const linkKindColor: Record<MapLink["kind"], ColorRole> = {
  inbound: "chart-2",
  transfer: "chart-1",
  sea_air: "chart-3",
  nearshore: "chart-4",
};

export function linkColorRole(link: Pick<MapLink, "kind" | "status" | "option_id">): ColorRole {
  return link.option_id === null && link.status !== "ok" ? statusMeta[link.status].color : linkKindColor[link.kind];
}

/** One drawn piece of a lane: a polyline (`path`) or a great-circle arc (`from` → `to`). */
export type PathLeg = { link: MapLink; path: LngLat[] };
export type ArcLeg = { link: MapLink; from: LngLat; to: LngLat };

/**
 * How a lane is drawn, following its `path` as given (M3 carry-over, `docs/contracts/90` §10):
 *
 * - inbound / transfer lanes: the whole path as a polyline;
 * - a sea–air lane with intermediate points (sea leg to the hub, e.g. Jebel Ali, then the flight):
 *   every leg up to the last point before the destination as a polyline, the final leg as an arc;
 * - a near-shore lane with intermediate points: the polyline as given (road / short sea);
 * - a two-point sea–air or near-shore lane: one arc (as in M3).
 */
export function laneLegs(links: readonly MapLink[]): { paths: PathLeg[]; arcs: ArcLeg[] } {
  const paths: PathLeg[] = [];
  const arcs: ArcLeg[] = [];
  for (const link of links) {
    const pts = link.path.map(toLngLat);
    if (pts.length < 2) continue;
    if (!ARC_KINDS.has(link.kind)) {
      paths.push({ link, path: pts });
    } else if (pts.length === 2) {
      arcs.push({ link, from: pts[0]!, to: pts[1]! });
    } else if (link.kind === "sea_air") {
      paths.push({ link, path: pts.slice(0, -1) });
      arcs.push({ link, from: pts[pts.length - 2]!, to: pts[pts.length - 1]! });
    } else {
      paths.push({ link, path: pts });
    }
  }
  return { paths, arcs };
}
