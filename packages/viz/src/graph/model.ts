/**
 * Pure model of the impact graph. The React Flow canvas and "View as table" are both built
 * from {@link buildGraphModel}, so they always list the same nodes for the selected filter.
 */
import type { ImpactGraphData, ImpactGraphEdge, ImpactGraphNode } from "@keelson/contracts";
import { formatValue } from "../format";
import { compareStatus, type NodeStatus } from "../lib/status";

/** Above this many edges the graph is not drawn (table only). Nodes are capped at 300 by the contract. */
export const GRAPH_LIMITS = { nodes: 300, edges: 2_000 } as const;

export type GraphFilter = "all" | NodeStatus;

export const graphNodeKindLabels: Record<ImpactGraphNode["kind"], string> = {
  supplier: "Supplier",
  component: "Component",
  factory: "Factory",
  port: "Port",
  chokepoint: "Chokepoint",
  lane: "Lane",
  po: "Purchase order",
  po_line: "PO line",
  dc: "Distribution centre",
  cluster: "Store cluster",
  store: "Store",
  style: "Style",
  sku: "SKU",
};

export const edgeKindLabels: Record<ImpactGraphEdge["kind"], string> = {
  supplies: "supplies",
  produces: "produces",
  ships: "ships to",
  stocks: "stocks",
  serves: "serves",
};

export type GraphModel = {
  nodes: ImpactGraphNode[];
  /** Edges between visible nodes (unknown endpoints and self-loops dropped, duplicates merged). */
  edges: ImpactGraphEdge[];
  /** id → ids it feeds / is fed by (visible edges only), in node order. */
  downstream: ReadonlyMap<string, string[]>;
  upstream: ReadonlyMap<string, string[]>;
};

export function edgeId(e: Pick<ImpactGraphEdge, "source" | "target" | "kind">): string {
  return `${e.source}->${e.target}:${e.kind}`;
}

export function statusCounts(data: ImpactGraphData): Map<NodeStatus, number> {
  const m = new Map<NodeStatus, number>();
  for (const n of data.nodes) m.set(n.status, (m.get(n.status) ?? 0) + 1);
  return m;
}

export function isGraphOversized(data: ImpactGraphData): boolean {
  return data.nodes.length > GRAPH_LIMITS.nodes || (data.edges?.length ?? 0) > GRAPH_LIMITS.edges;
}

export function buildGraphModel(data: ImpactGraphData, filter: GraphFilter): GraphModel {
  const seen = new Set<string>();
  const nodes = data.nodes
    .filter((n) => {
      if (seen.has(n.id)) return false; // duplicate ids would break the layout; first one wins
      seen.add(n.id);
      return filter === "all" || n.status === filter;
    })
    .sort((a, b) => compareStatus(a.status, b.status) || b.units_at_risk - a.units_at_risk || a.id.localeCompare(b.id));
  const ids = new Set(nodes.map((n) => n.id));
  const edgeIds = new Set<string>();
  const edges: ImpactGraphEdge[] = [];
  for (const e of data.edges ?? []) {
    if (e.source === e.target || !ids.has(e.source) || !ids.has(e.target)) continue;
    const id = edgeId(e);
    if (edgeIds.has(id)) continue;
    edgeIds.add(id);
    edges.push(e);
  }
  const order = new Map(nodes.map((n, i) => [n.id, i]));
  const downstream = new Map<string, string[]>();
  const upstream = new Map<string, string[]>();
  for (const e of edges) {
    downstream.set(e.source, [...(downstream.get(e.source) ?? []), e.target]);
    upstream.set(e.target, [...(upstream.get(e.target) ?? []), e.source]);
  }
  const byOrder = (a: string, b: string) => (order.get(a) ?? 0) - (order.get(b) ?? 0);
  for (const list of [...downstream.values(), ...upstream.values()]) list.sort(byOrder);
  return { nodes, edges, downstream, upstream };
}

/**
 * The node metric (`units_at_risk` in the payload) is the quantity on receipts the disruption
 * delays, not the shortfall: the impact graph calls it "Units on delayed receipts" (M3
 * carry-over, `docs/contracts/90` §10). The KPI strip keeps "Units at risk".
 */
export const IMPACT_UNITS_LABEL = "Units on delayed receipts";

/** "3,200 units on delayed receipts". */
export function delayedUnitsText(units: number): string {
  return `${formatValue(units, "units")} on delayed receipts`;
}
