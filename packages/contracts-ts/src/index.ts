/**
 * @keelson/contracts — Keelson wire contracts for the web app.
 *
 * - Zod schemas generated from `packages/contracts-ts/schemas` (do not hand-edit `src/generated`).
 * - Block envelope + per-kind props schemas/types and `parseBlockProps`.
 * - `parseAgUiEvent` (AG-UI 1.0 via `@ag-ui/core` `EventSchemas`) + `metadata.sc` extraction.
 */
import { llmDisruptionExtractSchema } from "./generated";

export * from "./blocks";
export * from "./agui";
export * as generated from "./generated";
export {
  llmFindingsSchema,
  llmIntakeResultSchema,
  llmRecommendationSchema,
  runRequestSchema,
  type LlmFindings,
  type LlmIntakeResult,
  type LlmRecommendation,
  type RunRequest,
} from "./generated";
// M5 (docs/contracts/95): admin Policies page, quorum progress, demo reset.
export {
  demoResetRequestSchema,
  demoResetResultSchema,
  policyContextSchema,
  policyDecisionSchema,
  policyQuorumSchema,
  policyRuleSchema,
  policySetSchema,
  type DemoResetRequest,
  type DemoResetResult,
  type PolicyContext,
  type PolicyDecision,
  type PolicyQuorum,
  type PolicyRule,
  type PolicySet,
} from "./generated";

// M6 (docs/contracts/100): Signal Hub — alerts inbox, alert detail drawer, presenter packs.
export {
  llmNewsExtractionSchema,
  signalsAlertDetailSchema,
  signalsAlertRuleSchema,
  signalsAlertSchema,
  signalsAlertStatusUpdateSchema,
  signalsEventSchema,
  signalsExposureSchema,
  signalsExposureSummarySchema,
  signalsScenarioPackSchema,
  signalsSignalSchema,
  signalsTenantNetworkSchema,
  type LlmNewsExtraction,
  type SignalsAlert,
  type SignalsAlertDetail,
  type SignalsAlertRule,
  type SignalsAlertStatusUpdate,
  type SignalsEvent,
  type SignalsExposure,
  type SignalsExposureSummary,
  type SignalsScenarioPack,
  type SignalsSignal,
  type SignalsTenantNetwork,
} from "./generated";
export * from "./signals";

// M7 (docs/contracts/110): back-office — four-eyes change requests, users, custom specialists +
// playground, tier-A read/action mappings + dry-run, per-op commit ledger, connector capabilities.
export {
  adminChangeRequestDecisionSchema,
  adminChangeRequestSchema,
  adminUserCreateSchema,
  adminUserCreatedSchema,
  adminUserPatchSchema,
  adminUserSchema,
  agentsCustomSpecialistSchema,
  agentsPlaygroundRequestSchema,
  agentsPlaygroundResultSchema,
  commitLedgerEntrySchema,
  connectorsCapabilitiesSchema,
  mappingActionSchema,
  mappingDryRunRequestSchema,
  mappingDryRunResultSchema,
  mappingReadSchema,
  scnSnapshotLabelSchema,
  type AdminChangeRequest,
  type AdminChangeRequestDecision,
  type AdminUser,
  type AdminUserCreate,
  type AdminUserCreated,
  type AdminUserPatch,
  type AgentsCustomSpecialist,
  type AgentsPlaygroundRequest,
  type AgentsPlaygroundResult,
  type CommitLedgerEntry,
  type ConnectorsCapabilities,
  type MappingAction,
  type MappingDryRunRequest,
  type MappingDryRunResult,
  type MappingRead,
  type ScnSnapshotLabel,
} from "./generated";
export * from "./backoffice";

// M8 (docs/contracts/121): mapping compiler explain + preview, proposed mappings.
export {
  mappingPlanSchema,
  mappingPreviewRequestSchema,
  mappingPreviewSchema,
  mappingProposalLlmSchema,
  mappingProposalSchema,
  mappingProposeRequestSchema,
  type MappingPlan,
  type MappingPreview,
  type MappingPreviewRequest,
  type MappingProposal,
  type MappingProposalLlm,
  type MappingProposeRequest,
} from "./generated";
export * from "./mapping-proposals";

/** Taxonomy event types (`data/taxonomy/events.yaml`; contracts-py `EVENT_TYPE_KEYS`). */
export const EVENT_TYPE_KEYS = llmDisruptionExtractSchema.shape.event_type.options;
export type EventTypeKey = (typeof EVENT_TYPE_KEYS)[number];
