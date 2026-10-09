/**
 * Block fixtures for the `/dev/blocks` demo page and tests (`import { blockFixtures } from "@keelson/viz/fixtures"`).
 * One valid envelope per block kind (M1 + M3 canvas visuals, the latter with their `data_ref`
 * payload in `data`), a few state variants, and negative cases (invalid props, unknown kind,
 * oversized data) that must render as RawDataCard.
 */
import type { BlockEnvelope, ImpactGraphData, NetworkMapData, PlanDiffRow } from "@keelson/contracts";
import alertOriginFeed from "../fixtures/alert_origin.feed.json";
import alertOrigin from "../fixtures/alert_origin.json";
import approachCardM4 from "../fixtures/approach_card.m4.json";
import approachCard from "../fixtures/approach_card.json";
import approvalCardBundle from "../fixtures/approval_card.bundle.json";
import approvalCardConnected from "../fixtures/approval_card.connected.json";
import approvalCardFourEyes from "../fixtures/approval_card.four_eyes.json";
import approvalCardRejected from "../fixtures/approval_card.rejected.json";
import approvalCard from "../fixtures/approval_card.json";
import commitTrackerApplying from "../fixtures/commit_tracker.applying.json";
import commitTrackerConflicts from "../fixtures/commit_tracker.conflicts.json";
import commitTracker from "../fixtures/commit_tracker.json";
import commitTrackerRolledBack from "../fixtures/commit_tracker.rolled_back.json";
import commitTrackerUndo from "../fixtures/commit_tracker.undo.json";
import evidencePack from "../fixtures/evidence_pack.json";
import impactGraphData from "../fixtures/impact_graph.data.json";
import impactGraph from "../fixtures/impact_graph.json";
import invalidKpiStrip from "../fixtures/invalid.kpi_strip.json";
import kpiStripFinal from "../fixtures/kpi_strip.final.json";
import kpiStrip from "../fixtures/kpi_strip.json";
import narrativeWhatChanged from "../fixtures/narrative.what_changed.json";
import narrative from "../fixtures/narrative.json";
import narrativeReplan from "../fixtures/narrative.replan.json";
import networkMapData from "../fixtures/network_map.data.json";
import networkMap from "../fixtures/network_map.json";
import planDiff from "../fixtures/plan_diff.json";
import recommendedOptionWhatIf from "../fixtures/recommended_option.what_if.json";
import recommendedOption from "../fixtures/recommended_option.json";
import recommendedOptionMixed from "../fixtures/recommended_option.mixed.json";
import remediationChecklistCompleted from "../fixtures/remediation_checklist.completed.json";
import remediationChecklist from "../fixtures/remediation_checklist.json";
import scenarioCompare from "../fixtures/scenario_compare.json";
import stockoutTimelineData from "../fixtures/stockout_timeline.data.json";
import stockoutTimeline from "../fixtures/stockout_timeline.json";
import type { BlockDataLoader } from "./types";
import unknownNetworkMap from "../fixtures/unknown.network_map.json";

export type BlockFixture = {
  /** Fixture file stem, e.g. "kpi_strip" or "approval_card.connected". */
  name: string;
  description: string;
  envelope: BlockEnvelope;
  /** `true` when the fixture must render as RawDataCard. */
  expectFallback?: boolean | undefined;
  /** The `data_ref` payload (`GET /api/blocks/{id}/data`) for data-backed kinds. */
  data?: unknown;
};

const env = (json: unknown) => json as BlockEnvelope;

/** 250-row plan diff (virtualised) generated from the base fixture. */
function largePlanDiff(): BlockEnvelope {
  const base = env(planDiff);
  const dcs = ["UK1", "NL1", "PL1"];
  const colours = ["BLK", "NAV", "OLV", "RED"];
  const sizes = ["XS", "S", "M", "L", "XL"];
  const rows: PlanDiffRow[] = Array.from({ length: 250 }, (_, i) => {
    const qty = 40 + ((i * 37) % 160);
    const sku = `PUF-${i % 2 ? "M" : "W"}-${colours[i % colours.length]}-${sizes[i % sizes.length]}`;
    return {
      seq: i + 1,
      op: "transfer_order.create",
      target_label: `${sku} · DE1 → ${dcs[i % dcs.length]}`,
      before_text: "No transfer",
      after_text: `Ship ${qty} on ${12 + (i % 5)} Oct`,
      qty,
      system: "Merxa",
    };
  });
  const units = rows.reduce((s, r) => s + (r.qty ?? 0), 0);
  return {
    ...base,
    id: "blk_plan_diff_large",
    title: "Changes in Merxa (large)",
    props: { plan_version: 3, rows, totals: { lines: rows.length, units, value_cents: units * 1350 } },
  };
}

/** Data-backed envelope with its own `data_ref` (as the engine emits it). */
function withId(json: unknown, id: string, patch: Partial<BlockEnvelope> = {}): BlockEnvelope {
  return { ...env(json), id, data_ref: `/api/blocks/${id}/data`, ...patch };
}

/** A plan block built on a crawled snapshot (M7 tier-A): the same props plus the advisory label. */
const ADVISORY_LABEL = {
  reason: "crawled_snapshot",
  text: "Data gathered by paging, not a consistent snapshot",
  as_of_range: { from: "2026-10-05T09:12:00Z", to: "2026-10-05T09:19:40Z" },
} as const;

function advisoryVariant(json: unknown, id: string): BlockEnvelope {
  const base = env(json);
  return { ...base, id, data_ref: null, props: { ...(base.props as object), advisory: ADVISORY_LABEL } } as BlockEnvelope;
}

/**
 * M8 (125): options planned with `integer_stage="cpsat"`: each option with re-sourcing notes how
 * its whole units were found (proven optimal, or the best found with its gap).
 */
function integerVariant(json: unknown, id: string): BlockEnvelope {
  const base = env(json);
  const props = base.props as { options: { id: string; levers: string[] }[] };
  const notes: Record<string, unknown> = {
    opt_nearshore: { method: "cpsat_optimal", gap_bp: 0, fallback_reason: null },
    opt_hybrid: { method: "cpsat_feasible", gap_bp: 40, fallback_reason: null },
  };
  const options = props.options.map((o) => (notes[o.id] ? { ...o, integer: notes[o.id] } : o));
  return { ...base, id, data_ref: null, props: { ...props, options } } as BlockEnvelope;
}

/** A network too large to draw (> 5,000 sites): the block shows paged tables instead. */
function largeNetwork(): { envelope: BlockEnvelope; data: NetworkMapData } {
  const statuses = ["ok", "ok", "ok", "at_risk", "impacted"] as const;
  const nodes = Array.from({ length: 5_200 }, (_, i) => ({
    id: `ST-${String(i + 1).padStart(4, "0")}`,
    kind: "store" as const,
    label: `Store ${i + 1}`,
    lat_e6: 48_000_000 + ((i * 7_919) % 7_000_000),
    lon_e6: -4_000_000 + ((i * 104_729) % 24_000_000),
    status: statuses[i % statuses.length]!,
    units_at_risk: i % 5 === 0 ? null : (i * 37) % 400,
  }));
  const base = env(networkMap);
  const props = base.props as { legend: unknown[] };
  return {
    envelope: withId(networkMap, "blk_network_map_large", {
      title: "Store network (large)",
      props: {
        ...props,
        title: "Store network (large)",
        summary: { nodes: nodes.length, at_risk_nodes: nodes.filter((n) => n.status !== "ok").length, lanes: 0 },
        bbox: [-4_000_000, 48_000_000, 20_000_000, 55_000_000],
      },
    }),
    data: { nodes, links: [], footprint: null },
  };
}

/** 301 nodes: over the contract's 300-node cap → the payload is rejected (RawDataCard, invalid_data). */
function oversizedImpactGraph(): ImpactGraphData {
  const base = impactGraphData as ImpactGraphData;
  const nodes = Array.from({ length: 301 }, (_, i) => ({ ...base.nodes[i % base.nodes.length]!, id: `n${i}` }));
  return { nodes, edges: [] };
}

const large = largeNetwork();

export const blockFixtures: BlockFixture[] = [
  { name: "narrative", description: "Composed narrative with an allowlisted Merxa link", envelope: env(narrative) },
  { name: "kpi_strip", description: "Preliminary exposure (priors)", envelope: env(kpiStrip) },
  { name: "kpi_strip.final", description: "Outcome with deltas", envelope: env(kpiStripFinal) },
  { name: "approach_card", description: "Approach interrupt: confirm / edit / what-if / cancel", envelope: env(approachCard) },
  { name: "recommended_option", description: "Recommended option with alternatives and actions", envelope: env(recommendedOption) },
  { name: "plan_diff", description: "Plan diff (9 lines)", envelope: env(planDiff) },
  { name: "plan_diff.large", description: "Plan diff, 250 lines (virtualised)", envelope: largePlanDiff() },
  { name: "approval_card", description: "Approval — Merxa not connected (Connect Merxa first)", envelope: env(approvalCard) },
  { name: "approval_card.connected", description: "Approval — Merxa connected", envelope: env(approvalCardConnected) },
  { name: "commit_tracker.applying", description: "Commit in progress", envelope: env(commitTrackerApplying) },
  { name: "commit_tracker", description: "Commit verified (read back)", envelope: env(commitTracker) },
  { name: "evidence_pack", description: "Evidence ledger (canvas)", envelope: env(evidencePack) },
  { name: "invalid.kpi_strip", description: "Invalid props → RawDataCard", envelope: env(invalidKpiStrip), expectFallback: true },
  { name: "unknown.network_map", description: "Unknown kind version → RawDataCard", envelope: env(unknownNetworkMap), expectFallback: true },
  // M3 canvas visuals (appended so the M1 order stays stable for e2e).
  { name: "scenario_compare", description: "Options on a Pareto scatter + scorecard (recommended, guide-only)", envelope: env(scenarioCompare) },
  { name: "network_map", description: "Network map: sites, lanes, proposed arcs, footprint", envelope: env(networkMap), data: networkMapData },
  { name: "impact_graph", description: "Impact graph (layered, keyboard navigable)", envelope: env(impactGraph), data: impactGraphData },
  {
    name: "stockout_timeline",
    description: "Stock-out heat strip with option switcher",
    envelope: env(stockoutTimeline),
    data: stockoutTimelineData,
  },
  {
    name: "network_map.empty",
    description: "Network map without sites yet",
    envelope: withId(networkMap, "blk_network_map_empty", {
      props: { ...(env(networkMap).props as object), summary: { nodes: 0, at_risk_nodes: 0, lanes: 0 } },
    }),
    data: { nodes: [], links: [], footprint: null },
  },
  { name: "network_map.large", description: "5,200 sites: too large to draw → paged tables", envelope: large.envelope, data: large.data },
  {
    name: "invalid.impact_graph.data",
    description: "Impact graph payload over 300 nodes → RawDataCard",
    envelope: withId(impactGraph, "blk_impact_graph_oversized"),
    data: oversizedImpactGraph(),
    expectFallback: true,
  },
  // M4 (docs/contracts/90), appended so the earlier order stays stable for e2e.
  {
    name: "approach_card.m4",
    description: "M4 approach: what-if, agents to consult, plan-task checks, typed Edit (days, receipts)",
    envelope: env(approachCardM4),
  },
  {
    name: "recommended_option.what_if",
    description: "What-if option: Apply off, Promote to actual, values to confirm",
    envelope: env(recommendedOptionWhatIf),
  },
  { name: "narrative.what_changed", description: "Follow-up narrative with its \"What changed\" line", envelope: env(narrativeWhatChanged) },
  // M5 (docs/contracts/95), appended so the earlier order stays stable for e2e.
  { name: "approval_card.four_eyes", description: "Four-eyes approval: 1 of 2 approvals, waiting for Compliance", envelope: env(approvalCardFourEyes) },
  { name: "approval_card.rejected", description: "Approval rejected with a reason", envelope: env(approvalCardRejected) },
  { name: "approval_card.bundle", description: "One approval for changes in two systems", envelope: env(approvalCardBundle) },
  { name: "recommended_option.mixed", description: "Mixed plan: Apply 3 changes for me · Guide me through 2", envelope: env(recommendedOptionMixed) },
  { name: "remediation_checklist", description: "Guide checklist: deep links, verified / done / pending steps, progress", envelope: env(remediationChecklist) },
  { name: "remediation_checklist.completed", description: "Guide completed (every step verified or skipped)", envelope: env(remediationChecklistCompleted) },
  { name: "commit_tracker.undo", description: "Verified changes with Undo changes", envelope: env(commitTrackerUndo) },
  { name: "commit_tracker.conflicts", description: "Undo refused: later changes on the same records", envelope: env(commitTrackerConflicts) },
  { name: "commit_tracker.rolled_back", description: "Changes rolled back", envelope: env(commitTrackerRolledBack) },
  { name: "narrative.replan", description: "Re-plan banner (v2) with plan versions and \"What changed\"", envelope: env(narrativeReplan) },
  // M6 (docs/contracts/100), appended so the earlier order stays stable for e2e.
  { name: "alert_origin", description: "Opened from alert (DEMO pack, two sources)", envelope: env(alertOrigin) },
  { name: "alert_origin.feed", description: "Opened from alert (live USGS feed)", envelope: env(alertOriginFeed) },
  // M7 (docs/contracts/110), appended so the earlier order stays stable for e2e.
  {
    name: "recommended_option.advisory",
    description: "Advisory option on crawled data (tier-A connector): apply mapped lines per op, guide the rest",
    envelope: advisoryVariant(recommendedOptionMixed, "blk_option_advisory"),
  },
  { name: "narrative.advisory", description: "Narrative of a plan built on crawled data (advisory note)", envelope: advisoryVariant(narrative, "blk_narrative_advisory") },
  {
    name: "scenario_compare.advisory",
    description: "Options compared on crawled data (Advisory badge + as-of range)",
    envelope: advisoryVariant(scenarioCompare, "blk_scenario_compare_advisory"),
  },
  // M8 (docs/contracts/125), appended so the earlier order stays stable for e2e.
  {
    name: "scenario_compare.integer",
    description: "CP-SAT plans: how each re-sourcing option's whole units were found (optimal, best found)",
    envelope: integerVariant(scenarioCompare, "blk_scenario_compare_integer"),
  },
];

export function getFixture(name: string): BlockFixture | undefined {
  return blockFixtures.find((f) => f.name === name);
}

/** The fixture payload behind a block id (`data_ref`), as `/api/blocks/{id}/data` would serve it. */
export function fixtureData(blockId: string): unknown {
  return blockFixtures.find((f) => f.envelope.id === blockId && f.data !== undefined)?.data;
}

/**
 * `BlockDataLoader` over the fixtures (for `/dev/blocks`): resolves after `delayMs` so the
 * loading state is visible; unknown ids reject like a 404.
 */
export function fixtureDataLoader(delayMs = 0): BlockDataLoader {
  return (envelope, { signal }) =>
    new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const data = fixtureData(envelope.id);
        if (data === undefined) reject(new Error(`No data for block ${envelope.id}`));
        else resolve(structuredClone(data));
      }, delayMs);
      signal.addEventListener("abort", () => {
        clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      });
    });
}
