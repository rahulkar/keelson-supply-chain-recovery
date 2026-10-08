import { z } from "zod";
import {
  blockAlertOriginV1PropsSchema,
  blockApproachCardV1PropsSchema,
  blockApprovalCardV1PropsSchema,
  blockCommitTrackerV1PropsSchema,
  blockEnvelopeSchema as generatedBlockEnvelopeSchema,
  blockEvidencePackV1PropsSchema,
  blockImpactGraphV1DataSchema,
  blockImpactGraphV1PropsSchema,
  blockKpiStripV1PropsSchema,
  blockNarrativeV1PropsSchema,
  blockNetworkMapV1DataSchema,
  blockNetworkMapV1PropsSchema,
  blockPlanDiffV1PropsSchema,
  blockProvenanceSchema,
  blockRecommendedOptionV1PropsSchema,
  blockRemediationChecklistV1PropsSchema,
  blockScenarioCompareV1PropsSchema,
  blockStockoutTimelineV1DataSchema,
  blockStockoutTimelineV1PropsSchema,
  type BlockEnvelope as GeneratedBlockEnvelope,
} from "./generated";

// ───────────────────────────── vocabularies ──────────────────────────────

/** Where a number or statement comes from (plan §3.3). */
export const PROVENANCE_KINDS = [
  "fact",
  "inference",
  "assumption",
  "simulated",
  "decision",
  "committed",
] as const;
export type Provenance = (typeof PROVENANCE_KINDS)[number];

/** Units used by KPI values (`kpi_strip@1`, `recommended_option@1`). */
export const VALUE_UNITS = ["units", "eur_cents", "days", "pct_bp", "count"] as const;
export type ValueUnit = (typeof VALUE_UNITS)[number];

export const BLOCK_PLACEMENTS = ["inline", "canvas"] as const;
export type BlockPlacement = (typeof BLOCK_PLACEMENTS)[number];

// ───────────────────────────── per-kind props ────────────────────────────

export const kpiStripPropsSchema = blockKpiStripV1PropsSchema;
export type KpiStripProps = z.infer<typeof kpiStripPropsSchema>;
export type KpiItem = KpiStripProps["items"][number];

export const approachCardPropsSchema = blockApproachCardV1PropsSchema;
export type ApproachCardProps = z.infer<typeof approachCardPropsSchema>;
export type ApproachDecision = ApproachCardProps["buttons"][number];

// M4 (docs/contracts/90 §2): agents to consult, checks per plan task, editable values.
// `checks` entries are `{task_id, sentence}` from M4 on; M1 cards carry plain strings.
export type ApproachCheckItem = ApproachCardProps["checks"][number];
export type ApproachCheck = Exclude<ApproachCheckItem, string>;
export type ApproachAgent = ApproachCardProps["agents"][number];
export type EditableValue = ApproachCardProps["editable"][number];
export type Assumption = ApproachCardProps["assumptions"][number];

/** The sentence of a check, whichever shape it has (M1 string or M4 `{task_id, sentence}`). */
export function checkSentence(check: ApproachCheckItem): string {
  return typeof check === "string" ? check : check.sentence;
}

/** The plan task id of a check (`null` for M1 plain-string checks). */
export function checkTaskId(check: ApproachCheckItem): string | null {
  return typeof check === "string" ? null : check.task_id;
}

export const recommendedOptionPropsSchema = blockRecommendedOptionV1PropsSchema;
export type RecommendedOptionProps = z.infer<typeof recommendedOptionPropsSchema>;
export type OptionAction = RecommendedOptionProps["actions"][number];
/** `apply` | `guide` | `confirm_assumptions` (M4) | `promote` (M4). */
export type OptionActionType = OptionAction["type"];
/** A value that must be confirmed before Apply is enabled (M4, docs/contracts/90 §5). */
export type UnconfirmedValue = RecommendedOptionProps["unconfirmed"][number];
/** Apply vs guide line counts ("Apply 3 changes for me · Guide me through 2"; M5, docs/contracts/95 §2). */
export type RemediationMix = NonNullable<RecommendedOptionProps["remediation_mix"]>;
/** M7: "Data gathered by paging, not a consistent snapshot" (also on `scenario_compare@1` and `narrative@1`). */
export type BlockAdvisory = NonNullable<RecommendedOptionProps["advisory"]>;

export const planDiffPropsSchema = blockPlanDiffV1PropsSchema;
export type PlanDiffProps = z.infer<typeof planDiffPropsSchema>;
export type PlanDiffRow = PlanDiffProps["rows"][number];

export const approvalCardPropsSchema = blockApprovalCardV1PropsSchema;
export type ApprovalCardProps = z.infer<typeof approvalCardPropsSchema>;
export type ApprovalDecision = ApprovalCardProps["buttons"][number];
// M5 (docs/contracts/95 §1, §5): quorum progress, rejection note, multi-system bundle members.
export type ApprovalQuorum = NonNullable<ApprovalCardProps["quorum"]>;
export type QuorumApproval = ApprovalQuorum["approvals"][number];
export type ApprovalRejection = NonNullable<ApprovalCardProps["rejection"]>;
export type ApprovalBundleMember = ApprovalCardProps["bundle"][number];

/** "1 of 2 approvals" — approvals so far and the total needed (approvals + remaining). */
export function quorumProgress(quorum: ApprovalQuorum): { approved: number; needed: number; met: boolean } {
  const approved = quorum.approvals.length;
  return { approved, needed: approved + quorum.remaining, met: quorum.remaining === 0 };
}

export const commitTrackerPropsSchema = blockCommitTrackerV1PropsSchema;
export type CommitTrackerProps = z.infer<typeof commitTrackerPropsSchema>;
/** `applying` | `applied` | `conflict` | `verified` | `rolled_back` (M5). */
export type CommitTrackerStatus = CommitTrackerProps["status"];
/** M5 tracker button (`undo` → case action `{type: "rollback", change_set_id}`). */
export type TrackerAction = CommitTrackerProps["actions"][number];
export type RollbackConflict = CommitTrackerProps["conflicts"][number];

export const narrativePropsSchema = blockNarrativeV1PropsSchema;
export type NarrativeProps = z.infer<typeof narrativePropsSchema>;
/** M5 re-plan banner ("New information: … — plan updated (v2)") and plan version switcher. */
export type ReplanBanner = NonNullable<NarrativeProps["banner"]>;
export type PlanVersionLink = NarrativeProps["plan_versions"][number];

// M5 Guide mode (docs/contracts/95 §2).
export const remediationChecklistPropsSchema = blockRemediationChecklistV1PropsSchema;
export type RemediationChecklistProps = z.infer<typeof remediationChecklistPropsSchema>;
export type ChecklistStep = RemediationChecklistProps["steps"][number];
/** `pending` | `done` | `verified` | `skipped` (`verified` only by read-back). */
export type ChecklistStepStatus = ChecklistStep["status"];

/** The guide is complete when every step is verified or skipped (a manual `done` still awaits read-back). */
export function checklistCompleted(props: Pick<RemediationChecklistProps, "steps">): boolean {
  return props.steps.every((s) => s.status === "verified" || s.status === "skipped");
}

// M6 (docs/contracts/100 §6, §8): "Opened from alert" origin card at the top of a case thread.
export const alertOriginPropsSchema = blockAlertOriginV1PropsSchema;
export type AlertOriginProps = z.infer<typeof alertOriginPropsSchema>;

export const evidencePackPropsSchema = blockEvidencePackV1PropsSchema;
export type EvidencePackProps = z.infer<typeof evidencePackPropsSchema>;
export type EvidenceItem = EvidencePackProps["items"][number];

// M3 canvas visuals (docs/contracts/80 §5). Coordinates are integer degrees × 1e6
// (`lat_e6`/`lon_e6`; positions are `[lon_e6, lat_e6]`), money is integer cents.

export const networkMapPropsSchema = blockNetworkMapV1PropsSchema;
export type NetworkMapProps = z.infer<typeof networkMapPropsSchema>;
export const networkMapDataSchema = blockNetworkMapV1DataSchema;
export type NetworkMapData = z.infer<typeof networkMapDataSchema>;
export type MapNode = NetworkMapData["nodes"][number];
export type MapLink = NetworkMapData["links"][number];

export const impactGraphPropsSchema = blockImpactGraphV1PropsSchema;
export type ImpactGraphProps = z.infer<typeof impactGraphPropsSchema>;
export const impactGraphDataSchema = blockImpactGraphV1DataSchema;
export type ImpactGraphData = z.infer<typeof impactGraphDataSchema>;
export type ImpactGraphNode = ImpactGraphData["nodes"][number];
export type ImpactGraphEdge = NonNullable<ImpactGraphData["edges"]>[number];

export const stockoutTimelinePropsSchema = blockStockoutTimelineV1PropsSchema;
export type StockoutTimelineProps = z.infer<typeof stockoutTimelinePropsSchema>;
export const stockoutTimelineDataSchema = blockStockoutTimelineV1DataSchema;
export type StockoutTimelineData = z.infer<typeof stockoutTimelineDataSchema>;
export type StockoutRow = StockoutTimelineData["rows"][number];

export const scenarioComparePropsSchema = blockScenarioCompareV1PropsSchema;
export type ScenarioCompareProps = z.infer<typeof scenarioComparePropsSchema>;
export type CompareOption = ScenarioCompareProps["options"][number];
export type Scorecard = CompareOption["scorecard"];

/** Converts an integer `*_e6` coordinate to degrees (for map libraries). */
export function fromE6(value: number): number {
  return value / 1_000_000;
}

export { blockProvenanceSchema };
export type BlockProvenance = z.infer<typeof blockProvenanceSchema>;

/** Props schema per `kind@v` for every block kind this package knows. */
export const blockPropsSchemas = {
  "kpi_strip@1": kpiStripPropsSchema,
  "approach_card@1": approachCardPropsSchema,
  "recommended_option@1": recommendedOptionPropsSchema,
  "plan_diff@1": planDiffPropsSchema,
  "approval_card@1": approvalCardPropsSchema,
  "commit_tracker@1": commitTrackerPropsSchema,
  "narrative@1": narrativePropsSchema,
  "evidence_pack@1": evidencePackPropsSchema,
  "network_map@1": networkMapPropsSchema,
  "impact_graph@1": impactGraphPropsSchema,
  "stockout_timeline@1": stockoutTimelinePropsSchema,
  "scenario_compare@1": scenarioComparePropsSchema,
  "remediation_checklist@1": remediationChecklistPropsSchema,
  "alert_origin@1": alertOriginPropsSchema,
} as const;

/** Schema of the payload behind `data_ref` (`GET /api/blocks/{id}/data`) per `kind@v`. */
export const blockDataSchemas = {
  "network_map@1": networkMapDataSchema,
  "impact_graph@1": impactGraphDataSchema,
  "stockout_timeline@1": stockoutTimelineDataSchema,
} as const;

export type BlockDataKey = keyof typeof blockDataSchemas;
export type BlockDataByKey = { [K in BlockDataKey]: z.infer<(typeof blockDataSchemas)[K]> };

/** Block kinds that may only be placed on the canvas. */
export const CANVAS_ONLY_KINDS = [
  "evidence_pack",
  "network_map",
  "impact_graph",
  "stockout_timeline",
  "scenario_compare",
] as const;

export type BlockKey = keyof typeof blockPropsSchemas;
export type BlockPropsByKey = { [K in BlockKey]: z.infer<(typeof blockPropsSchemas)[K]> };
export type BlockKind = BlockKey extends `${infer K}@${number}` ? K : never;
export const BLOCK_KEYS = Object.keys(blockPropsSchemas) as BlockKey[];

export function blockKey(kind: string, v: number): `${string}@${number}` {
  return `${kind}@${v}`;
}

export function isKnownBlockKey(key: string): key is BlockKey {
  return Object.prototype.hasOwnProperty.call(blockPropsSchemas, key);
}

// ───────────────────────────── envelopes ─────────────────────────────────

/**
 * The block envelope as it travels to the UI (`ACTIVITY_SNAPSHOT.content`, `GET /api/cases/{id}`).
 *
 * Deliberately lenient about `props` (and tolerant of a missing/odd provenance) so that
 * one bad or newer block never breaks a thread: `BlockRenderer` validates `props` per
 * `kind@v` and falls back to the raw-data card. Use {@link knownBlockEnvelopeSchema}
 * when you need the strict, fully-typed union.
 */
export const blockEnvelopeSchema = z.object({
  id: z.string().min(1),
  kind: z.string().min(1),
  v: z.number().int().min(1),
  title: z.string().catch(""),
  placement: z.enum(BLOCK_PLACEMENTS).catch("inline"),
  provenance: z
    .object({
      kind: z.string(),
      engine: z.string().optional(),
      inputs_hash: z.string().nullable().optional(),
      as_of: z.string().optional(),
    })
    .loose()
    .nullable()
    .catch(null),
  data_ref: z.string().nullable().catch(null),
  props: z.unknown(),
});
export type BlockEnvelope = z.infer<typeof blockEnvelopeSchema>;

/** Strict union over every known `kind@v` (generated from contracts-py). */
export const knownBlockEnvelopeSchema = generatedBlockEnvelopeSchema;
export type KnownBlockEnvelope = GeneratedBlockEnvelope;

/** Lenient parse of a block envelope; `null` when it is not even envelope-shaped. */
export function toBlockEnvelope(value: unknown): BlockEnvelope | null {
  const parsed = blockEnvelopeSchema.safeParse(value);
  return parsed.success ? parsed.data : null;
}

export type BlockPropsResult<K extends BlockKey = BlockKey> =
  | { ok: true; key: K; props: BlockPropsByKey[K] }
  | { ok: false; key: string; reason: "unknown_kind" | "invalid_props"; issues: string[] };

/** Validate `envelope.props` against the schema for its `kind@v`. */
export function parseBlockProps(envelope: Pick<BlockEnvelope, "kind" | "v" | "props">): BlockPropsResult {
  const key = blockKey(envelope.kind, envelope.v);
  if (!isKnownBlockKey(key)) {
    return { ok: false, key, reason: "unknown_kind", issues: [`No renderer for block kind "${key}"`] };
  }
  const schema = blockPropsSchemas[key] as z.ZodType<BlockPropsByKey[typeof key]>;
  const parsed = schema.safeParse(envelope.props);
  if (!parsed.success) {
    return { ok: false, key, reason: "invalid_props", issues: formatIssues(parsed.error) };
  }
  return { ok: true, key, props: parsed.data } as BlockPropsResult;
}

export function hasBlockData(key: string): key is BlockDataKey {
  return Object.prototype.hasOwnProperty.call(blockDataSchemas, key);
}

export type BlockDataResult<K extends BlockDataKey = BlockDataKey> =
  | { ok: true; key: K; data: BlockDataByKey[K] }
  | { ok: false; key: string; reason: "no_data_schema" | "invalid_data"; issues: string[] };

/** Validate a `data_ref` payload (fetched from `/api/blocks/{id}/data`) for its `kind@v`. */
export function parseBlockData(envelope: Pick<BlockEnvelope, "kind" | "v">, data: unknown): BlockDataResult {
  const key = blockKey(envelope.kind, envelope.v);
  if (!hasBlockData(key)) {
    return { ok: false, key, reason: "no_data_schema", issues: [`Block kind "${key}" has no data_ref payload`] };
  }
  const schema = blockDataSchemas[key] as z.ZodType<BlockDataByKey[typeof key]>;
  const parsed = schema.safeParse(data);
  if (!parsed.success) {
    return { ok: false, key, reason: "invalid_data", issues: formatIssues(parsed.error) };
  }
  return { ok: true, key, data: parsed.data } as BlockDataResult;
}

/** Human-readable one-liners for Zod issues (`path: message`). */
export function formatIssues(error: z.ZodError): string[] {
  return error.issues.map((issue) => {
    const where = issue.path.length ? issue.path.map(String).join(".") : "(root)";
    return `${where}: ${issue.message}`;
  });
}
