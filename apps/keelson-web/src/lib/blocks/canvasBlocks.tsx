"use client";

/**
 * The M3 canvas visuals, loaded on demand with `next/dynamic` and never rendered on the server
 * (MapLibre / deck.gl need WebGL, React Flow and ECharts measure the DOM). Importing this module
 * (side effect) replaces the package's default lazy registrations in the `@keelson/viz` registry.
 */
import { blockPropsSchemas, type BlockPropsByKey } from "@keelson/contracts";
import { BlockSkeleton, configureNetworkMap, registerBlock, type BlockComponentProps, type CanvasBlockKey } from "@keelson/viz";
import dynamic from "next/dynamic";
import type { ComponentType } from "react";
import type { z } from "zod";

/** Same-origin MapLibre worker, copied into `public/maplibre/` by `scripts/copy-maplibre-worker.mjs`. */
export const MAPLIBRE_WORKER_URL = "/maplibre/maplibre-gl-worker.mjs";

configureNetworkMap({ workerUrl: MAPLIBRE_WORKER_URL });

type AnyBlock = ComponentType<BlockComponentProps<never, never>>;

const NetworkMap = dynamic(() => import("@keelson/viz/network-map"), {
  ssr: false,
  loading: () => <BlockSkeleton title="network map" />,
}) as unknown as AnyBlock;

const ImpactGraph = dynamic(() => import("@keelson/viz/impact-graph"), {
  ssr: false,
  loading: () => <BlockSkeleton title="impact graph" />,
}) as unknown as AnyBlock;

const StockoutTimeline = dynamic(() => import("@keelson/viz/stockout-timeline"), {
  ssr: false,
  loading: () => <BlockSkeleton title="stock-out timeline" />,
}) as unknown as AnyBlock;

const ScenarioCompare = dynamic(() => import("@keelson/viz/scenario-compare"), {
  ssr: false,
  loading: () => <BlockSkeleton title="option comparison" />,
}) as unknown as AnyBlock;

export const canvasBlockComponents: Record<CanvasBlockKey, AnyBlock> = {
  "network_map@1": NetworkMap,
  "impact_graph@1": ImpactGraph,
  "stockout_timeline@1": StockoutTimeline,
  "scenario_compare@1": ScenarioCompare,
};

for (const [key, component] of Object.entries(canvasBlockComponents) as [CanvasBlockKey, AnyBlock][]) {
  registerBlock<BlockPropsByKey[typeof key]>(key, {
    schema: blockPropsSchemas[key] as unknown as z.ZodType<BlockPropsByKey[typeof key]>,
    component: component as unknown as ComponentType<BlockComponentProps<BlockPropsByKey[typeof key]>>,
  });
}

