"use client";

/**
 * Read-only workflow graph (React Flow 12 + elkjs layered layout) for the admin Agents page
 * (`docs/contracts/110` §3). Nothing can be dragged, connected or deleted; the same content is
 * always available as a list (`WorkflowOutline`). Load it with `next/dynamic(…, { ssr: false })`.
 */
import "@xyflow/react/dist/style.css";
import { Badge } from "@keelson/ui";
import { hexColor, type ThemeName } from "@keelson/tokens";
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useDocumentTheme } from "../lib/theme";
import { WORKFLOW_KIND_LABELS, buildWorkflowModel, workflowNodeAriaLabel, type WorkflowInput, type WorkflowModel, type WorkflowNode } from "./model";

export const WORKFLOW_NODE_SIZE = { width: 196, height: 64 } as const;

type Positions = Map<string, { x: number; y: number }>;

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

/** Layered top-to-bottom layout (deterministic for the same graph). */
export async function layoutWorkflow(model: WorkflowModel): Promise<Positions> {
  const elk = await loadElk();
  const out = await elk.layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.layered.spacing.nodeNodeBetweenLayers": "40",
      "elk.spacing.nodeNode": "24",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
      "elk.randomSeed": "1",
    },
    children: model.nodes.map((n) => ({ id: n.id, width: WORKFLOW_NODE_SIZE.width, height: WORKFLOW_NODE_SIZE.height })),
    edges: model.edges.map((e, i) => ({ id: `e${i}`, sources: [e.source], targets: [e.target] })),
  });
  const positions: Positions = new Map();
  for (const c of out.children ?? []) positions.set(c.id, { x: c.x ?? 0, y: c.y ?? 0 });
  return positions;
}

type FlowData = { node: WorkflowNode };
type FlowNode = Node<FlowData, "wf">;

const KIND_TONE = { entry: "neutral", llm: "accent", fn: "neutral", gate: "warning", specialist: "info" } as const;

function WorkflowNodeView({ data }: NodeProps<FlowNode>) {
  const n = data.node;
  return (
    <div
      className={`flex h-full w-full flex-col justify-between rounded-md border bg-surface px-2.5 py-1.5 text-left ${n.enabled ? "border-border-strong" : "border-dashed border-border opacity-60"}`}
      style={{ width: WORKFLOW_NODE_SIZE.width, height: WORKFLOW_NODE_SIZE.height }}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} className="size-1.5! min-h-0! min-w-0! border-0! bg-border-strong!" />
      <div className="flex items-start justify-between gap-1.5">
        <span className="line-clamp-1 text-caption leading-4 font-medium text-fg" title={n.label}>
          {n.label}
        </span>
        <Badge size="sm" tone={n.enabled ? KIND_TONE[n.kind] : "neutral"}>
          {!n.enabled ? (n.kind === "specialist" ? "Disabled" : "Off") : n.custom ? "Custom" : WORKFLOW_KIND_LABELS[n.kind]}
        </Badge>
      </div>
      {n.detail ? <span className="line-clamp-1 text-[0.6875rem] leading-4 text-fg-muted">{n.detail}</span> : null}
      <Handle type="source" position={Position.Bottom} isConnectable={false} className="size-1.5! min-h-0! min-w-0! border-0! bg-border-strong!" />
    </div>
  );
}

const nodeTypes = { wf: WorkflowNodeView };

export type WorkflowGraphProps = {
  input: WorkflowInput;
  height?: number | undefined;
  /** Theme for edge colours (default: the document's). */
  theme?: ThemeName | undefined;
};

export default function WorkflowGraph({ input, height = 560, theme: themeProp }: WorkflowGraphProps) {
  const docTheme = useDocumentTheme();
  const theme = themeProp ?? docTheme;
  const model = useMemo(() => buildWorkflowModel(input), [input]);
  const key = useMemo(() => JSON.stringify([model.nodes.map((n) => n.id), model.edges.map((e) => `${e.source}>${e.target}`)]), [model]);
  const [layout, setLayout] = useState<{ key: string; positions: Positions } | { key: string; error: string } | null>(null);
  const latestModel = useRef(model);
  useEffect(() => {
    latestModel.current = model;
  });

  // Lay out again only when the structure changes (`key`), not for a new object with the same graph.
  useEffect(() => {
    let cancelled = false;
    layoutWorkflow(latestModel.current)
      .then((positions) => {
        if (!cancelled) setLayout({ key, positions });
      })
      .catch((err: unknown) => {
        if (!cancelled) setLayout({ key, error: err instanceof Error ? err.message : "layout failed" });
      });
    return () => {
      cancelled = true;
    };
  }, [key]);

  const current = layout && layout.key === key ? layout : null;
  const positions = current && "positions" in current ? current.positions : null;

  const nodes = useMemo<FlowNode[]>(
    () =>
      positions
        ? model.nodes.map((n) => ({
            id: n.id,
            type: "wf" as const,
            position: positions.get(n.id) ?? { x: 0, y: 0 },
            data: { node: n },
            width: WORKFLOW_NODE_SIZE.width,
            height: WORKFLOW_NODE_SIZE.height,
            draggable: false,
            connectable: false,
            deletable: false,
            selectable: false,
            ariaLabel: workflowNodeAriaLabel(n),
          }))
        : [],
    [positions, model.nodes],
  );

  const edges = useMemo<Edge[]>(() => {
    const stroke = hexColor("border-strong", theme);
    return model.edges.map((e, i) => ({
      id: `e${i}`,
      source: e.source,
      target: e.target,
      type: "smoothstep",
      focusable: false,
      selectable: false,
      deletable: false,
      ...(e.label ? { label: e.label, labelStyle: { fontSize: 11 } } : {}),
      style: { stroke, strokeWidth: 1.25, ...(e.dashed ? { strokeDasharray: "4 4" } : {}) },
      markerEnd: { type: MarkerType.ArrowClosed, color: stroke, width: 12, height: 12 },
    }));
  }, [model.edges, theme]);

  if (current && "error" in current) {
    return <p className="text-ui text-fg-muted">The graph couldn&apos;t be laid out ({current.error}); the list below has every step.</p>;
  }

  return (
    <div
      role="group"
      aria-label="Workflow graph (read-only)"
      className="overflow-hidden rounded-md border border-border bg-surface-2"
      style={{ height }}
      data-workflow-graph=""
      aria-busy={!positions || undefined}
    >
      {positions ? (
        <ReactFlow<FlowNode>
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          colorMode={theme}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          edgesFocusable={false}
          deleteKeyCode={null}
          zoomOnDoubleClick={false}
          minZoom={0.3}
          maxZoom={1.5}
          fitView
          fitViewOptions={{ padding: 0.1 }}
        >
          <Background gap={16} size={1} color={hexColor("border", theme)} />
          <Controls showInteractive={false} />
        </ReactFlow>
      ) : (
        <p role="status" className="p-3 text-ui text-fg-muted">
          Laying out the workflow…
        </p>
      )}
    </div>
  );
}
