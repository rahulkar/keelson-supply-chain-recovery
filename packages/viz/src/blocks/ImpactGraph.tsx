"use client";

import type { ImpactGraphData, ImpactGraphNode, ImpactGraphProps } from "@keelson/contracts";
import { Table, usePrefersReducedMotion, type TableColumn } from "@keelson/ui";
import { Workflow } from "lucide-react";
import { lazy, Suspense, useId, useMemo, useState } from "react";
import { formatValue } from "../format";
import { ChoiceChips, Notice, ViewToggle, type VisualView } from "../lib/controls";
import { BlockSkeleton } from "../lib/lazyBlock";
import { StatusBadge } from "../lib/StatusBadge";
import { NODE_STATUSES, statusMeta } from "../lib/status";
import { useDocumentTheme, useIsClient } from "../lib/theme";
import { weekLabel } from "../lib/week";
import { buildGraphModel, graphNodeKindLabels, GRAPH_LIMITS, IMPACT_UNITS_LABEL, isGraphOversized, statusCounts, type GraphFilter, type GraphModel } from "../graph/model";
import type { BlockComponentProps } from "../types";
import { BlockFrame } from "./BlockFrame";

const GraphCanvas = lazy(() => import("../graph/GraphCanvas"));

const GRAPH_HEIGHT = { canvas: 480, inline: 300 } as const;

function names(ids: readonly string[] | undefined, labels: ReadonlyMap<string, string>): string {
  if (!ids || ids.length === 0) return "—";
  const shown = ids.slice(0, 4).map((id) => labels.get(id) ?? id);
  return ids.length > 4 ? `${shown.join(", ")} and ${ids.length - 4} more` : shown.join(", ");
}

/** Impact graph nodes as a table (same nodes, same order as the drawn graph). */
export function ImpactGraphTable({ model }: { model: GraphModel }) {
  const labels = useMemo(() => new Map(model.nodes.map((n) => [n.id, n.label || n.id])), [model.nodes]);
  const columns: TableColumn<ImpactGraphNode>[] = [
    { id: "label", header: "Node", cell: (n) => n.label || n.id, rowHeader: true },
    { id: "kind", header: "Type", cell: (n) => graphNodeKindLabels[n.kind], nowrap: true },
    { id: "status", header: "Status", cell: (n) => <StatusBadge status={n.status} />, nowrap: true },
    { id: "units", header: IMPACT_UNITS_LABEL, cell: (n) => formatValue(n.units_at_risk, "units"), align: "end", nowrap: true },
    { id: "breach", header: "First stock-out", cell: (n) => (n.first_breach_week ? weekLabel(n.first_breach_week) : "—"), nowrap: true },
    { id: "fed_by", header: "Fed by", cell: (n) => names(model.upstream.get(n.id), labels) },
    { id: "feeds", header: "Feeds", cell: (n) => names(model.downstream.get(n.id), labels) },
  ];
  return (
    <Table
      caption={`Impact graph nodes (${formatValue(model.nodes.length, "count")})`}
      columns={columns}
      rows={model.nodes}
      rowKey={(n) => n.id}
      rowProps={(n) => ({ "data-node-id": n.id })}
      empty="No nodes for this filter."
    />
  );
}

/**
 * `impact_graph@1` (canvas) — how the disruption propagates: suppliers → factories → lanes →
 * POs → DCs → clusters, each node with its status badge, units on delayed receipts and first stock-out week.
 * Layered layout (elkjs) in React Flow, keyboard navigable; "View as table" lists the same nodes.
 */
export function ImpactGraph({ props, data, envelope, density }: BlockComponentProps<ImpactGraphProps, ImpactGraphData>) {
  const theme = useDocumentTheme();
  const reducedMotion = usePrefersReducedMotion();
  const isClient = useIsClient();
  const regionId = `graph-${useId()}`;
  const [view, setView] = useState<VisualView>("visual");
  const [filter, setFilter] = useState<GraphFilter>("all");

  const counts = useMemo(() => statusCounts(data), [data]);
  const selected: GraphFilter = filter === "all" || counts.has(filter) ? filter : "all";
  const model = useMemo(() => buildGraphModel(data, selected), [data, selected]);
  const oversized = isGraphOversized(data);
  const empty = data.nodes.length === 0;
  const canDraw = !oversized && !empty;
  const shown: VisualView = canDraw ? view : "table";
  const { summary } = props;

  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle="Impact graph"
      icon={<Workflow />}
      actions={
        canDraw ? (
          <ViewToggle view={view} onChange={setView} visualLabel="graph" visualIcon={<Workflow aria-hidden="true" />} controls={regionId} />
        ) : undefined
      }
    >
      <p className="text-ui text-fg-muted">
        {formatValue(summary.nodes, "count")} nodes ·{" "}
        <span className="font-medium text-fg">
          {formatValue(summary.disrupted, "count")} disrupted · {formatValue(summary.impacted, "count")} impacted
        </span>
      </p>

      {counts.size > 1 ? (
        <ChoiceChips
          label="Show nodes"
          value={selected}
          onChange={(id) => setFilter(id as GraphFilter)}
          choices={[
            { id: "all", label: "All", count: data.nodes.length },
            ...NODE_STATUSES.filter((s) => counts.has(s)).map((s) => ({ id: s, label: statusMeta[s].label, count: counts.get(s) })),
          ]}
        />
      ) : null}

      {empty ? (
        <p className="text-ui text-fg-muted">No impact to show yet.</p>
      ) : (
        <div id={regionId} className="flex flex-col gap-2">
          {oversized ? (
            <Notice>
              This graph is too large to draw (more than {formatValue(GRAPH_LIMITS.edges, "count")} links), so it is shown as a table.
            </Notice>
          ) : null}
          {shown === "visual" && isClient ? (
            <Suspense
              fallback={<BlockSkeleton title="graph" className={`w-full rounded-md ${density === "canvas" ? "h-[480px]" : "h-[300px]"}`} />}
            >
              <GraphCanvas
                model={model}
                theme={theme}
                reducedMotion={reducedMotion}
                height={density === "canvas" ? GRAPH_HEIGHT.canvas : GRAPH_HEIGHT.inline}
              />
            </Suspense>
          ) : shown === "visual" ? (
            <BlockSkeleton title="graph" className="h-[300px] w-full rounded-md" />
          ) : (
            <ImpactGraphTable model={model} />
          )}
        </div>
      )}
    </BlockFrame>
  );
}

export default ImpactGraph;
