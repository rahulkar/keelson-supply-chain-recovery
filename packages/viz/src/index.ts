/**
 * @keelson/viz — render-block registry for Keelson.
 *
 * `<BlockRenderer envelope onAction busy density />` validates `envelope.props` per `kind@v`
 * and renders the M1 blocks and the M3 canvas visuals (lazily loaded; `data_ref` payloads are
 * resolved with `loadData` and validated before they render); anything unknown or invalid
 * becomes a `RawDataCard`. Canvas visuals: `@keelson/viz/{network-map,impact-graph,
 * stockout-timeline,scenario-compare}` (default export = block component).
 * Styles: import "@keelson/viz/styles.css" in the app stylesheet.
 * Fixtures for `/dev/blocks`: `import { blockFixtures } from "@keelson/viz/fixtures"`.
 */
export { BlockRenderer, mergeBlockData } from "./BlockRenderer";
export {
  CANVAS_BLOCK_KEYS,
  canvasBlockLoaders,
  type CanvasBlockKey,
  blockRegistry,
  getBlockDefinition,
  registerBlock,
  registeredBlockKeys,
  type BlockDefinition,
  type RegistryKey,
} from "./registry";
export type {
  AnyCaseActionType,
  ApproachEditPayload,
  ApproachEditValue,
  BlockAction,
  CaseActionType,
  ManualStepStatus,
  BlockComponentProps,
  BlockDataLoader,
  BlockDensity,
  BlockRendererProps,
  BlockDecision,
  BlockViewer,
  ViewerConnection,
} from "./types";

export { formatDate, formatDateTime, formatDelta, formatValue, opLabel, type FormatOptions, type Unit } from "./format";
export { ALLOWED_LINK_HOSTS, ALLOWED_LINK_PREFIXES, currentReturnTo, isAllowedHref } from "./links";

export { ApprovalCard, DECISION_REASON_MAX, approvalConnectState, type ApprovalConnectState } from "./blocks/ApprovalCard";
export { EXECUTOR_ROLE, approvalNeedsLink, quorumSummary, viewerApproval, type QuorumSummary } from "./blocks/quorum";
export { roleLabel, roleList } from "./lib/roles";
export { ApproachCard, eventTypeLabel, listSentence } from "./blocks/ApproachCard";
export {
  APPROACH_EDIT_LIMITS,
  approachEditDraft,
  approachEditResult,
  hasTypedEdit,
  parseIdList,
  type ApproachEditDraft,
  type ApproachEditResult,
} from "./blocks/approachEdit";
export { BlockFrame, type BlockFrameProps } from "./blocks/BlockFrame";
export { CommitTracker, DONE_STEPS, STATUS_TEXT as COMMIT_STATUS_TEXT, undoAction } from "./blocks/CommitTracker";
export { RemediationChecklist, checklistAnnouncement, checklistProgress, safeDeepLink } from "./blocks/RemediationChecklist";
export {
  PlanVersionSwitcher,
  ReplanNotice,
  comparePlanDiffs,
  comparisonSentence,
  isDeltaOf,
  planStatusLabel,
  type PlanComparison,
  type PlanVersionView,
} from "./blocks/PlanVersions";
export { EvidencePack } from "./blocks/EvidencePack";
export { KpiStrip, KpiTiles, type KpiTilesProps } from "./blocks/KpiStrip";
export { Narrative, NarrativeMarkdown, NARRATIVE_REHYPE_PLUGINS, WhatChanged, type NarrativeMarkdownProps } from "./blocks/Narrative";
export {
  PLAN_DIFF_INLINE_ROWS,
  PLAN_DIFF_VIRTUALIZE_THRESHOLD,
  PlanDiff,
  PlanDiffTable,
  type PlanDiffTableProps,
} from "./blocks/PlanDiff";
export { RawDataCard, type RawDataCardProps, type RawDataReason } from "./blocks/RawDataCard";
export {
  RecommendedOption,
  UNCONFIRMED_APPLY_REASON,
  WHAT_IF_APPLY_REASON,
  confirmAction,
  mixSentence,
  optionActions,
} from "./blocks/RecommendedOption";
export { DecidedNote, decidedText, decisionLabel, formatDeciders } from "./blocks/DecidedNote";
export { CODE_PATTERN, NoBreakCodes, splitCodes } from "./lib/noBreak";
export { BarChart, type BarChartProps, type BarDatum } from "./charts/BarChart";

// M3 canvas visuals: helpers that do not pull in the heavy libraries (those load on demand).
export { BlockSkeleton, lazyBlock } from "./lib/lazyBlock";
export { configureNetworkMap, DEFAULT_NETWORK_MAP_CONFIG, networkMapConfig, type NetworkMapConfig } from "./map/config";
export { NODE_STATUSES, statusMeta, type NodeStatus } from "./lib/status";
export { breachShift, breachShiftText, isoWeekMonday, weekLabel, weeksBetween, type BreachShift } from "./lib/week";

// M6 (docs/contracts/100 §8): "Opened from alert" card + the alert vocabulary shared with the Alerts inbox.
export { AlertOrigin, alertHref } from "./blocks/AlertOrigin";
export { DemoBadge, KIND_META, KindIcon, SEVERITY_META, SeverityBadge, humanize, kindLabel, severityRank } from "./lib/alerts";

// M7 (docs/contracts/110): advisory labels on plans from crawled data; read-only workflow graph
// model (the drawn graph is `@keelson/viz/workflow-graph`, load it client-side only).
export { ADVISORY_FALLBACK_TEXT, AdvisoryBadge, AdvisoryNote, advisoryRangeText, type AdvisoryLabel } from "./blocks/Advisory";
export {
  FIXED_NODES as WORKFLOW_FIXED_NODES,
  WORKFLOW_KIND_LABELS,
  buildWorkflowModel,
  specialistNodeId,
  workflowNodeAriaLabel,
  type WorkflowEdge,
  type WorkflowInput,
  type WorkflowModel,
  type WorkflowNode,
  type WorkflowNodeKind,
  type WorkflowSpecialist,
} from "./workflow/model";
