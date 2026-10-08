"use client";

/**
 * The drawn part of the impact graph: React Flow 12 with an elkjs layered layout, status
 * badges on every node and arrow-key navigation along the supply chain. Loaded on demand by
 * `ImpactGraph`; everything it shows is also in "View as table".
 */
import "@xyflow/react/dist/style.css";
import type { ImpactGraphNode } from "@keelson/contracts";
import { hexColor, type ThemeName } from "@keelson/tokens";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import { useEffect, useMemo, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
import { formatValue } from "../format";
import { StatusBadge } from "../lib/StatusBadge";
import { statusHex, statusLabel } from "../lib/status";
import { weekLabel } from "../lib/week";
import { layoutGraph, neighbour, NODE_SIZE, type Positions } from "./layout";
import { delayedUnitsText, edgeId, edgeKindLabels, graphNodeKindLabels, type GraphModel } from "./model";

type ImpactNodeData = { node: ImpactGraphNode; theme: ThemeName };
type ImpactFlowNode = Node<ImpactNodeData, "impact">;

function ImpactNode({ data }: NodeProps<ImpactFlowNode>) {
  const { node, theme } = data;
  return (
    <div
      className="flex h-full w-full flex-col justify-between gap-1 rounded-md border bg-surface px-2.5 py-2 text-left"
      style={{ borderColor: statusHex(node.status, theme), borderLeftWidth: 4, width: NODE_SIZE.width, height: NODE_SIZE.height }}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} className="size-1.5! min-h-0! min-w-0! border-0! bg-border-strong!" />
      <div className="flex items-start justify-between gap-2">
        <span className="line-clamp-2 text-caption leading-4 font-medium text-fg" title={node.label}>
          {node.label || node.id}
        </span>
        <StatusBadge status={node.status} />
      </div>
      <span className="truncate text-[0.6875rem] leading-4 text-fg-muted">
        {graphNodeKindLabels[node.kind]}
        {node.units_at_risk > 0 ? ` · ${formatValue(node.units_at_risk, "units")} delayed` : ""}
        {node.first_breach_week ? ` · short ${node.first_breach_week.slice(5)}` : ""}
      </span>
      <Handle type="source" position={Position.Right} isConnectable={false} className="size-1.5! min-h-0! min-w-0! border-0! bg-border-strong!" />
    </div>
  );
}

const nodeTypes = { impact: ImpactNode };

export function nodeAriaLabel(n: ImpactGraphNode): string {
  const parts = [`${graphNodeKindLabels[n.kind]} ${n.label || n.id}`, statusLabel(n.status)];
  if (n.units_at_risk > 0) parts.push(delayedUnitsText(n.units_at_risk));
  if (n.first_breach_week) parts.push(`first stock-out ${weekLabel(n.first_breach_week)}`);
  return parts.join(", ");
}

const ARROW_KEYS = new Set(["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"]);

/** Below this zoom node text is unreadable: wide graphs open at this zoom, left-aligned (where the disruption starts). */
export const MIN_READABLE_ZOOM = 0.6;
const PAD = 24;

export const GRAPH_HINT = "Drag to move around; Tab to a node, then arrow keys follow the supply chain.";

/**
 * Initial viewport: the whole graph when it fits at a readable zoom, otherwise a readable zoom
 * that starts at the left (upstream) edge, vertically placed on the nodes that are in view there
 * (the upstream columns can sit far below the graph's overall top: on the full profile the first
 * column started ~1,400 px down and a top-anchored view opened on empty canvas).
 */
export function initialViewport(
  positions: Positions,
  width: number,
  height: number,
): { fit: true } | { fit: false; x: number; y: number; zoom: number } {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of positions.values()) {
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + NODE_SIZE.width);
    maxY = Math.max(maxY, p.y + NODE_SIZE.height);
  }
  if (!Number.isFinite(minX)) return { fit: true };
  const fitZoom = Math.min((width - 2 * PAD) / (maxX - minX), (height - 2 * PAD) / (maxY - minY));
  if (fitZoom >= MIN_READABLE_ZOOM) return { fit: true };
  const zoom = MIN_READABLE_ZOOM;
  // Nodes that lie (fully) inside the horizontal extent shown from the left edge at this zoom.
  const visibleRight = minX + (width - 2 * PAD) / zoom;
  let top = Infinity;
  let bottom = -Infinity;
  for (const p of positions.values()) {
    if (p.x + NODE_SIZE.width <= visibleRight) {
      top = Math.min(top, p.y);
      bottom = Math.max(bottom, p.y + NODE_SIZE.height);
    }
  }
  if (!Number.isFinite(top)) {
    top = minY;
    bottom = maxY;
  }
  const span = (bottom - top) * zoom;
  const y = span + 2 * PAD <= height ? (height - span) / 2 - top * zoom : PAD - top * zoom;
  return { fit: false, x: PAD - minX * zoom, y, zoom };
}

export type GraphCanvasProps = {
  model: GraphModel;
  theme: ThemeName;
  reducedMotion: boolean;
  height: number;
};

export default function GraphCanvas({ model, theme, reducedMotion, height }: GraphCanvasProps) {
  const host = useRef<HTMLDivElement>(null);
  const layoutKey = useMemo(
    () => JSON.stringify([model.nodes.map((n) => n.id), model.edges.map(edgeId)]),
    [model.nodes, model.edges],
  );
  const [layout, setLayout] = useState<{ key: string; positions: Positions } | { key: string; error: string } | null>(null);
  const [announce, setAnnounce] = useState(GRAPH_HINT);
  const latestModel = useRef(model);
  useEffect(() => {
    latestModel.current = model;
  });

  // Lay out again only when the graph's structure changes (node ids / edges), not when the same
  // graph arrives as a new object (re-emitted block, statuses updated): ELK runs on the main thread.
  useEffect(() => {
    let cancelled = false;
    const { nodes: layoutNodes, edges: layoutEdges } = latestModel.current;
    layoutGraph(layoutNodes, layoutEdges)
      .then((positions) => {
        if (!cancelled) setLayout({ key: layoutKey, positions });
      })
      .catch((err: unknown) => {
        if (!cancelled) setLayout({ key: layoutKey, error: err instanceof Error ? err.message : "layout failed" });
      });
    return () => {
      cancelled = true;
    };
  }, [layoutKey]);

  const current = layout && layout.key === layoutKey ? layout : null;
  const positions = current && "positions" in current ? current.positions : null;
  const byId = useMemo(() => new Map(model.nodes.map((n) => [n.id, n])), [model.nodes]);

  const nodes = useMemo<ImpactFlowNode[]>(
    () =>
      positions
        ? model.nodes.map((n) => ({
            id: n.id,
            type: "impact" as const,
            position: positions.get(n.id) ?? { x: 0, y: 0 },
            data: { node: n, theme },
            width: NODE_SIZE.width,
            height: NODE_SIZE.height,
            draggable: false,
            connectable: false,
            deletable: false,
            ariaLabel: nodeAriaLabel(n),
          }))
        : [],
    [positions, model.nodes, theme],
  );

  const edges = useMemo<Edge[]>(() => {
    const stroke = hexColor("border-strong", theme);
    return model.edges.map((e) => {
      const target = byId.get(e.target);
      const hot = target && target.status !== "ok" ? statusHex(target.status, theme) : stroke;
      return {
        id: edgeId(e),
        source: e.source,
        target: e.target,
        type: "smoothstep",
        focusable: false,
        selectable: false,
        deletable: false,
        ariaLabel: `${byId.get(e.source)?.label ?? e.source} ${edgeKindLabels[e.kind]} ${target?.label ?? e.target}`,
        style: { stroke: hot, strokeWidth: 1.5 },
        markerEnd: { type: MarkerType.ArrowClosed, color: hot, width: 14, height: 14 },
      };
    });
  }, [model.edges, byId, theme]);

  const focusNode = (id: string) => {
    const el = host.current?.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(id)}"]`);
    el?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!positions || !ARROW_KEYS.has(event.key)) return;
    const el = (event.target as HTMLElement).closest<HTMLElement>(".react-flow__node");
    const id = el?.dataset.id;
    if (!id) return;
    event.preventDefault();
    event.stopPropagation();
    const next = neighbour(id, event.key as "ArrowRight", positions, model.downstream, model.upstream);
    if (next) {
      focusNode(next);
    } else {
      const where = { ArrowRight: "feeds nothing further", ArrowLeft: "has nothing upstream", ArrowDown: "is last in its column", ArrowUp: "is first in its column" }[
        event.key as "ArrowRight"
      ];
      setAnnounce(`${byId.get(id)?.label ?? id} ${where}.`);
    }
  };

  const onFocus = (event: FocusEvent<HTMLDivElement>) => {
    const id = (event.target as HTMLElement).closest<HTMLElement>(".react-flow__node")?.dataset.id;
    const n = id ? byId.get(id) : undefined;
    if (!n || !id) return;
    const feeds = (model.downstream.get(id) ?? []).map((t) => byId.get(t)?.label ?? t);
    const fedBy = (model.upstream.get(id) ?? []).map((t) => byId.get(t)?.label ?? t);
    setAnnounce(
      [
        nodeAriaLabel(n),
        feeds.length ? `feeds ${feeds.slice(0, 3).join(", ")}${feeds.length > 3 ? ` and ${feeds.length - 3} more` : ""}` : "",
        fedBy.length ? `fed by ${fedBy.slice(0, 3).join(", ")}${fedBy.length > 3 ? ` and ${fedBy.length - 3} more` : ""}` : "",
      ]
        .filter(Boolean)
        .join("; "),
    );
  };

  const onInit = (instance: ReactFlowInstance<ImpactFlowNode>) => {
    const el = host.current;
    if (!el || !positions) return;
    const view = initialViewport(positions, el.clientWidth, el.clientHeight);
    if (view.fit) void instance.fitView({ padding: 0.12, duration: 0 });
    else void instance.setViewport({ x: view.x, y: view.y, zoom: view.zoom }, { duration: 0 });
  };

  if (current && "error" in current) {
    return <p className="text-ui text-fg-muted">The graph couldn&apos;t be laid out ({current.error}); use &quot;View as table&quot;.</p>;
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div
        ref={host}
        className="overflow-hidden rounded-md border border-border bg-surface-2"
        style={{ height }}
        onKeyDown={onKeyDown}
        onFocus={onFocus}
        data-graph-canvas=""
        aria-busy={!positions || undefined}
      >
        {positions ? (
          <ReactFlow<ImpactFlowNode>
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            colorMode={theme}
            onInit={onInit}
            fitViewOptions={{ padding: 0.12, duration: reducedMotion ? 0 : 200 }}
            minZoom={0.15}
            maxZoom={1.6}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable
            nodesFocusable
            edgesFocusable={false}
            deleteKeyCode={null}
            zoomOnScroll={false}
            panOnScroll={false}
            preventScrolling={false}
            zoomOnDoubleClick={false}
            autoPanOnNodeFocus
            ariaLabelConfig={{
              "node.a11yDescription.default":
                "Arrow keys follow the supply chain: right to what this feeds, left to what feeds it, up and down within a column.",
              "node.a11yDescription.keyboardDisabled":
                "Arrow keys follow the supply chain: right to what this feeds, left to what feeds it, up and down within a column.",
            }}
          >
            <Background gap={20} size={1} color={hexColor("border", theme)} />
            <Controls showInteractive={false} position="bottom-left" />
          </ReactFlow>
        ) : null}
      </div>
      <p aria-live="polite" className="min-h-4 text-caption text-fg-muted" data-graph-announce="">
        {announce}
      </p>
    </div>
  );
}
