import { blockPropsSchemas, type BlockKey, type BlockPropsByKey } from "@keelson/contracts";
import type { ComponentType } from "react";
import type { z } from "zod";
import { AlertOrigin } from "./blocks/AlertOrigin";
import { ApprovalCard } from "./blocks/ApprovalCard";
import { ApproachCard } from "./blocks/ApproachCard";
import { CommitTracker } from "./blocks/CommitTracker";
import { EvidencePack } from "./blocks/EvidencePack";
import { KpiStrip } from "./blocks/KpiStrip";
import { Narrative } from "./blocks/Narrative";
import { PlanDiff } from "./blocks/PlanDiff";
import { RecommendedOption } from "./blocks/RecommendedOption";
import { RemediationChecklist } from "./blocks/RemediationChecklist";
import { lazyBlock } from "./lib/lazyBlock";
import type { BlockComponentProps } from "./types";

export type RegistryKey = `${string}@${number}`;

export type BlockDefinition<P = unknown> = {
  /** Zod schema for `envelope.props`. */
  schema: z.ZodType<P>;
  component: ComponentType<BlockComponentProps<P>>;
};

function def<K extends BlockKey>(
  key: K,
  component: ComponentType<BlockComponentProps<BlockPropsByKey[K]>>,
): [K, BlockDefinition<BlockPropsByKey[K]>] {
  return [key, { schema: blockPropsSchemas[key] as unknown as z.ZodType<BlockPropsByKey[K]>, component }];
}

/**
 * M3 canvas visuals, loaded on demand (MapLibre / deck.gl / React Flow / elkjs / ECharts stay
 * out of the main bundle). Next.js apps re-register them with `next/dynamic(…, { ssr: false })`
 * from the `@keelson/viz/<visual>` entry points.
 */
export const canvasBlockLoaders = {
  "network_map@1": () => import("./blocks/NetworkMap"),
  "impact_graph@1": () => import("./blocks/ImpactGraph"),
  "stockout_timeline@1": () => import("./blocks/StockoutTimeline"),
  "scenario_compare@1": () => import("./blocks/ScenarioCompare"),
} as const;

export type CanvasBlockKey = keyof typeof canvasBlockLoaders;
export const CANVAS_BLOCK_KEYS = Object.keys(canvasBlockLoaders) as CanvasBlockKey[];

const canvasTitles: Record<CanvasBlockKey, string> = {
  "network_map@1": "network map",
  "impact_graph@1": "impact graph",
  "stockout_timeline@1": "stock-out timeline",
  "scenario_compare@1": "option comparison",
};

function lazyDef<K extends CanvasBlockKey>(key: K): [K, BlockDefinition<BlockPropsByKey[K]>] {
  const component = lazyBlock<BlockPropsByKey[K], unknown>(
    canvasBlockLoaders[key] as unknown as () => Promise<{ default: ComponentType<BlockComponentProps<BlockPropsByKey[K], unknown>> }>,
    canvasTitles[key],
  );
  return [key, { schema: blockPropsSchemas[key] as unknown as z.ZodType<BlockPropsByKey[K]>, component }];
}

const definitions = new Map<RegistryKey, BlockDefinition<never>>(
  [
    def("kpi_strip@1", KpiStrip),
    def("approach_card@1", ApproachCard),
    def("recommended_option@1", RecommendedOption),
    def("plan_diff@1", PlanDiff),
    def("approval_card@1", ApprovalCard),
    def("commit_tracker@1", CommitTracker),
    def("narrative@1", Narrative),
    def("evidence_pack@1", EvidencePack),
    def("remediation_checklist@1", RemediationChecklist),
    def("alert_origin@1", AlertOrigin),
    ...CANVAS_BLOCK_KEYS.map((k) => lazyDef(k)),
  ] as unknown as [RegistryKey, BlockDefinition<never>][],
);

/** `kind@v` → component (M1 blocks + lazily loaded M3 canvas visuals). {@link registerBlock} adds or replaces kinds. */
export const blockRegistry: Record<RegistryKey, ComponentType<BlockComponentProps<never>>> = Object.fromEntries(
  [...definitions].map(([k, d]) => [k, d.component]),
);

/** Full definition (schema + component) for a key, if registered. */
export function getBlockDefinition(key: string): BlockDefinition<unknown> | undefined {
  return definitions.get(key as RegistryKey) as BlockDefinition<unknown> | undefined;
}

/** Register (or replace) a block kind at runtime, e.g. `registerBlock("network_map@1", { schema, component })`. */
export function registerBlock<P>(key: RegistryKey, definition: BlockDefinition<P>): void {
  definitions.set(key, definition as unknown as BlockDefinition<never>);
  blockRegistry[key] = definition.component as unknown as ComponentType<BlockComponentProps<never>>;
}

export function registeredBlockKeys(): RegistryKey[] {
  return [...definitions.keys()];
}
