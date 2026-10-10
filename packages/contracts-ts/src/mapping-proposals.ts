/**
 * M8 mapping compiler + proposed mappings vocabularies and small helpers (docs/contracts/121;
 * contracts-py `keelson_contracts.mapping` / `.mapping_proposal`). The schemas are generated
 * (`mappingPlanSchema`, `mappingPreviewSchema`, `mappingProposalSchema`, … in ./generated); this module
 * derives the enums the admin UI shows and mirrors the aggregate table of the expression language.
 */
import { mappingPreviewRequestSchema, mappingProposalSchema, mappingProposeRequestSchema } from "./generated";

/** Aggregates a `group_by` entity mapping's fields may use (only as the whole expression; `mapping.AGGREGATE_FUNCTIONS`). */
export const MAPPING_AGGREGATE_FUNCTIONS = {
  first: [1, 1],
  min: [1, 1],
  max: [1, 1],
  sum: [1, 1],
  count: [0, 0],
} as const satisfies Record<string, readonly [number, number]>;

/** Most `group_by` keys of one entity mapping (`mapping.MAX_GROUP_BY`). */
export const MAPPING_MAX_GROUP_BY = 4;

/** `live` (vendor, ≤ 3 pages per source) | `staged` (the last crawl's raw pages, no vendor call). */
export const PREVIEW_SOURCES = mappingPreviewRequestSchema.shape.source.unwrap().options;
export type PreviewSource = (typeof PREVIEW_SOURCES)[number];

/** `heuristic` | `llm` | `both` — who proposed a field, lookup, entity or op. */
export const PROPOSAL_ORIGINS = ["heuristic", "llm", "both"] as const;
export type ProposalOrigin = (typeof PROPOSAL_ORIGINS)[number];

/** `ready` | `accepted` | `discarded`. */
export const PROPOSAL_STATUSES = mappingProposalSchema.shape.status.options;
export type ProposalStatus = (typeof PROPOSAL_STATUSES)[number];

/** `heuristic` | `llm+heuristic`. */
export const PROPOSERS = mappingProposalSchema.shape.proposer.options;

/** `auto` (rules, refined by the AI when available) | `heuristic` (rules only, never a model call). */
export const PROPOSE_MODES = mappingProposeRequestSchema.shape.proposer.unwrap().options;

/** Every proposal note code (`mapping_proposal.ProposalNoteCode`). */
export const PROPOSAL_NOTE_CODES = mappingProposalSchema.shape.notes.unwrap().element.shape.code.options;
export type ProposalNoteCode = (typeof PROPOSAL_NOTE_CODES)[number];

/** The LLM's coarse confidence in basis points (`mapping_proposal.CONFIDENCE_BP`). */
export const CONFIDENCE_BP = { high: 8500, medium: 6000, low: 3000 } as const;

export type ConfidenceTone = "high" | "medium" | "low";

/** How a confidence (basis points) is shown: ≥ 75 % high, ≥ 50 % medium, else low (always with its percentage). */
export function confidenceTone(bp: number): ConfidenceTone {
  if (bp >= 7500) return "high";
  if (bp >= 5000) return "medium";
  return "low";
}

/** `8500` → `"85%"` (rounded down: never shows more confidence than there is). */
export function confidencePercent(bp: number): string {
  return `${Math.floor(Math.max(0, Math.min(10_000, bp)) / 100)}%`;
}
