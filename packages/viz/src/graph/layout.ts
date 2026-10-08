/**
 * Layered left-to-right layout with elkjs (loaded on demand; the bundled build runs in the
 * page, no worker, no eval). Deterministic for the same graph.
 */
import type { ImpactGraphEdge, ImpactGraphNode } from "@keelson/contracts";
import { edgeId } from "./model";

export const NODE_SIZE = { width: 208, height: 72 } as const;

export type Positions = Map<string, { x: number; y: number }>;

type ElkModule = typeof import("elkjs/lib/elk.bundled.js");
let elkLoading: Promise<InstanceType<ElkModule["default"]>> | null = null;

function loadElk() {
  elkLoading ??= import("elkjs/lib/elk.bundled.js")
    .then((m) => new m.default())
    .catch((err: unknown) => {
      elkLoading = null;
      throw err;
    });
  return elkLoading;
}

export async function layoutGraph(nodes: readonly ImpactGraphNode[], edges: readonly ImpactGraphEdge[]): Promise<Positions> {
  const elk = await loadElk();
  const out = await elk.layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "72",
      "elk.spacing.nodeNode": "20",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
      "elk.randomSeed": "1",
    },
    children: nodes.map((n) => ({ id: n.id, width: NODE_SIZE.width, height: NODE_SIZE.height })),
    edges: edges.map((e) => ({ id: edgeId(e), sources: [e.source], targets: [e.target] })),
  });
  const positions: Positions = new Map();
  for (const c of out.children ?? []) positions.set(c.id, { x: c.x ?? 0, y: c.y ?? 0 });
  return positions;
}

/**
 * Keyboard neighbour for arrow-key navigation: right → first node it feeds, left → first node
 * feeding it, down / up → next / previous node in the same column (by position).
 */
export function neighbour(
  id: string,
  key: "ArrowRight" | "ArrowLeft" | "ArrowDown" | "ArrowUp",
  positions: Positions,
  downstream: ReadonlyMap<string, string[]>,
  upstream: ReadonlyMap<string, string[]>,
): string | null {
  const here = positions.get(id);
  if (!here) return null;
  const byY = (ids: readonly string[]) =>
    [...ids].sort((a, b) => (positions.get(a)?.y ?? 0) - (positions.get(b)?.y ?? 0) || a.localeCompare(b));
  if (key === "ArrowRight") return byY(downstream.get(id) ?? [])[0] ?? null;
  if (key === "ArrowLeft") return byY(upstream.get(id) ?? [])[0] ?? null;
  const column = byY([...positions.entries()].filter(([, p]) => Math.abs(p.x - here.x) < 1).map(([k]) => k));
  const i = column.indexOf(id);
  const next = key === "ArrowDown" ? column[i + 1] : column[i - 1];
  return next ?? null;
}
