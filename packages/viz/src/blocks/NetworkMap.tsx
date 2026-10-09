"use client";

import type { MapLink, MapNode, NetworkMapData, NetworkMapProps } from "@keelson/contracts";
import { hexColor } from "@keelson/tokens";
import { Table, usePrefersReducedMotion, type TableColumn } from "@keelson/ui";
import { Map as MapIcon } from "lucide-react";
import { lazy, Suspense, useId, useMemo, useState } from "react";
import { formatValue } from "../format";
import { ChoiceChips, Notice, usePaged, ViewToggle, type VisualView } from "../lib/controls";
import { BlockSkeleton } from "../lib/lazyBlock";
import { StatusBadge } from "../lib/StatusBadge";
import { statusMeta, type NodeStatus } from "../lib/status";
import { useDocumentTheme, useIsClient } from "../lib/theme";
import {
  ALL_OPTIONS,
  buildMapModel,
  CURRENT_ONLY,
  isOversized,
  linkKindColor,
  linkKindLabels,
  mapOptions,
  nodeKindLabels,
  type MapModel,
} from "../map/model";
import { useNetworkMapSettings } from "../map/settings";
import { useBasemap } from "../map/useBasemap";
import { supportsWebGL2 } from "../map/webgl";
import type { BlockComponentProps } from "../types";
import { BlockFrame } from "./BlockFrame";

const MapCanvas = lazy(() => import("../map/MapCanvas"));

const MAP_HEIGHT = { canvas: 440, inline: 280 } as const;

function Swatch({ color, shape = "dot" }: { color: string; shape?: "dot" | "line" | "area" }) {
  const cls = shape === "dot" ? "size-2.5 rounded-pill" : shape === "line" ? "h-1 w-4 rounded-pill" : "size-3 rounded-xs opacity-60";
  return <span aria-hidden="true" className={`inline-block shrink-0 ${cls}`} style={{ backgroundColor: color }} />;
}

/**
 * Legend from `props.legend`. A `link` entry whose key is a plan option id (`option_id` of a lane)
 * names that option (chip label) and is not drawn as a swatch.
 */
function Legend({
  legend: all,
  theme,
  optionIds,
}: {
  legend: NetworkMapProps["legend"];
  theme: "light" | "dark";
  optionIds: ReadonlySet<string>;
}) {
  const legend = all.filter((i) => !(i.kind === "link" && optionIds.has(i.key)));
  if (legend.length === 0) return null;
  const colorFor = (item: NetworkMapProps["legend"][number]): string => {
    // Own keys only: a legend key such as "constructor" must not hit Object.prototype.
    if (Object.hasOwn(statusMeta, item.key)) return hexColor(statusMeta[item.key as NodeStatus].color, theme);
    if (Object.hasOwn(linkKindColor, item.key)) return hexColor(linkKindColor[item.key as MapLink["kind"]], theme);
    return hexColor("fg-subtle", theme);
  };
  return (
    <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-caption text-fg-muted">
      {legend.map((item) => (
        <li key={`${item.kind}:${item.key}`} className="inline-flex items-center gap-1.5">
          <Swatch color={colorFor(item)} shape={item.kind === "node" ? "dot" : item.kind === "link" ? "line" : "area"} />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

/** Sites and lanes as tables: exactly what the map draws for the selected option. */
export function NetworkTables({
  model,
  labels,
  optionTitles,
}: {
  model: MapModel;
  labels: ReadonlyMap<string, string>;
  optionTitles: ReadonlyMap<string, string>;
}) {
  const sites = usePaged(model.nodes);
  const lanes = usePaged(model.links);
  const siteColumns: TableColumn<MapNode>[] = [
    { id: "label", header: "Site", cell: (n) => n.label || n.id, rowHeader: true },
    { id: "kind", header: "Type", cell: (n) => nodeKindLabels[n.kind], nowrap: true },
    { id: "status", header: "Status", cell: (n) => <StatusBadge status={n.status} />, nowrap: true },
    {
      id: "units",
      header: "Units at risk",
      cell: (n) => (n.units_at_risk === null ? "—" : formatValue(n.units_at_risk, "units")),
      align: "end",
      nowrap: true,
    },
  ];
  const laneColumns: TableColumn<MapLink>[] = [
    {
      id: "lane",
      header: "Lane",
      cell: (l) => `${labels.get(l.from) ?? l.from} → ${labels.get(l.to) ?? l.to}`,
      rowHeader: true,
    },
    { id: "kind", header: "Type", cell: (l) => linkKindLabels[l.kind], nowrap: true },
    { id: "status", header: "Status", cell: (l) => <StatusBadge status={l.status} />, nowrap: true },
    { id: "units", header: "Units", cell: (l) => (l.units === null ? "—" : formatValue(l.units, "units")), align: "end", nowrap: true },
    {
      id: "option",
      header: "Plan option",
      cell: (l) => (l.option_id ? (optionTitles.get(l.option_id) ?? l.option_id) : "Current network"),
    },
  ];
  return (
    <div className="flex flex-col gap-3" data-map-table="">
      <Table caption={`Sites (${formatValue(model.nodes.length, "count")})`} columns={siteColumns} rows={sites.visible} rowKey={(n) => n.id} empty="No sites." />
      {sites.more}
      <Table
        caption={`Lanes (${formatValue(model.links.length, "count")})`}
        columns={laneColumns}
        rows={lanes.visible}
        rowKey={(l) => l.id}
        empty="No lanes for this selection."
        rowProps={(l) => ({ "data-link-id": l.id })}
      />
      {lanes.more}
    </div>
  );
}

/**
 * `network_map@1` (canvas) — sites coloured by status, inbound / transfer lanes, proposed
 * sea–air and near-shore arcs and the disruption footprint (MapLibre + deck.gl, loaded on demand)
 * on a base map from the fallback chain (docs/contracts/127-m8-basemap.md): self-hosted Protomaps
 * when installed, OpenFreeMap, the built-in Natural Earth outline, a plain background. Option chips
 * filter the proposed lanes; "View as table" lists exactly what the map shows. Without WebGL2, or
 * for payloads too large to draw, the tables are shown instead.
 */
export function NetworkMap({ props, data, envelope, density }: BlockComponentProps<NetworkMapProps, NetworkMapData>) {
  const theme = useDocumentTheme();
  const reducedMotion = usePrefersReducedMotion();
  const isClient = useIsClient();
  const regionId = `map-${useId()}`;
  const [view, setView] = useState<VisualView>("visual");
  const [option, setOption] = useState<string>(ALL_OPTIONS);
  const settings = useNetworkMapSettings();

  const options = useMemo(() => mapOptions(data, props.legend), [data, props.legend]);
  const optionTitles = useMemo(() => new Map(options.map((o) => [o.id, o.label])), [options]);
  const optionIds = useMemo(() => new Set(options.map((o) => o.id)), [options]);
  const labels = useMemo(() => new Map(data.nodes.map((n) => [n.id, n.label || n.id])), [data.nodes]);
  const selected = option === ALL_OPTIONS || option === CURRENT_ONLY || optionTitles.has(option) ? option : ALL_OPTIONS;
  const model = useMemo(() => buildMapModel(data, selected), [data, selected]);

  const empty = data.nodes.length === 0 && data.links.length === 0;
  const oversized = useMemo(() => isOversized(data), [data]);
  const webgl = isClient && supportsWebGL2();
  const canDraw = webgl && !oversized && !empty;
  const shown: VisualView = canDraw ? view : "table";
  const basemap = useBasemap(settings, shown === "visual");
  const basemapKind = basemap.kind;
  const { summary } = props;
  const skeleton = <BlockSkeleton title="map" className={`w-full rounded-md ${density === "canvas" ? "h-[440px]" : "h-[280px]"}`} />;

  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle={props.title || "Network map"}
      icon={<MapIcon />}
      actions={
        canDraw ? (
          <ViewToggle view={view} onChange={setView} visualLabel="map" visualIcon={<MapIcon aria-hidden="true" />} controls={regionId} />
        ) : undefined
      }
    >
      <p className="text-ui text-fg-muted">
        {formatValue(summary.nodes, "count")} {summary.nodes === 1 ? "site" : "sites"} ·{" "}
        <span className="font-medium text-fg">{formatValue(summary.at_risk_nodes, "count")} at risk</span> ·{" "}
        {formatValue(summary.lanes, "count")} {summary.lanes === 1 ? "lane" : "lanes"}
      </p>

      {options.length > 0 ? (
        <ChoiceChips
          label="Plan option lanes"
          value={selected}
          onChange={setOption}
          choices={[
            { id: ALL_OPTIONS, label: "All options" },
            { id: CURRENT_ONLY, label: "Current network" },
            ...options.map((o) => ({ id: o.id, label: o.label, count: o.links })),
          ]}
        />
      ) : null}

      {empty ? (
        <p className="text-ui text-fg-muted">No sites or lanes to show yet.</p>
      ) : (
        <div id={regionId} className="flex flex-col gap-2">
          {oversized ? (
            <Notice>
              This network is too large to draw ({formatValue(data.nodes.length, "count")} sites, {formatValue(data.links.length, "count")}{" "}
              lanes), so it is shown as tables.
            </Notice>
          ) : isClient && !webgl ? (
            <Notice>This browser can&apos;t draw the map (WebGL is unavailable), so the network is shown as tables.</Notice>
          ) : null}
          {shown === "visual" ? (
            <>
              {basemapKind === "plain" ? (
                <Notice>The background map couldn&apos;t be loaded; sites and lanes are drawn without it.</Notice>
              ) : basemapKind === "offline" && settings.mode !== "offline" ? (
                <Notice>Showing a simplified background map: the detailed map isn&apos;t available.</Notice>
              ) : null}
              <div
                role="group"
                aria-label={`Map of ${model.nodes.length} sites and ${model.links.length} lanes; ${summary.at_risk_nodes} at risk. "View as table" lists the details.`}
                data-map-canvas=""
                data-map-basemap={basemapKind ?? undefined}
              >
                {basemapKind === null ? (
                  skeleton
                ) : (
                  <Suspense fallback={skeleton}>
                    <MapCanvas
                      // A new base map kind remounts the canvas (fresh MapLibre map and load state).
                      key={basemapKind}
                      model={model}
                      bbox={props.bbox}
                      theme={theme}
                      reducedMotion={reducedMotion}
                      labels={labels}
                      optionTitles={optionTitles}
                      height={density === "canvas" ? MAP_HEIGHT.canvas : MAP_HEIGHT.inline}
                      basemap={{ kind: basemapKind, installed: basemap.installed }}
                      onBaseMapError={() => basemap.fail(basemapKind)}
                    />
                  </Suspense>
                )}
              </div>
            </>
          ) : (
            <NetworkTables model={model} labels={labels} optionTitles={optionTitles} />
          )}
        </div>
      )}
      <Legend legend={props.legend} theme={theme} optionIds={optionIds} />
    </BlockFrame>
  );
}

export default NetworkMap;
