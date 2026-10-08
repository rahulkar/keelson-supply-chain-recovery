/**
 * M7 back-office vocabularies and small helpers (docs/contracts/110; contracts-py `keelson_contracts.admin`,
 * `.agents`, `.mapping`, `.commit`, `.connectors`, `.scn`). Schemas are generated (`admin*Schema`,
 * `agents*Schema`, `mapping*Schema`, … in ./generated); this module only derives friendly names and
 * mirrors a few pure rules the admin UI shows before the engine answers (the engine stays authoritative).
 */
import {
  adminChangeRequestSchema,
  adminUserCreateSchema,
  commitLedgerEntrySchema,
  connectorsCapabilitiesSchema,
  scnSnapshotLabelSchema,
  type AdminChangeRequest,
} from "./generated";

/** Keelson product roles (Keycloak realm `keelson`); only these are shown or assigned. */
export const PRODUCT_ROLES = adminUserCreateSchema.shape.roles.element.options;
export type ProductRole = (typeof PRODUCT_ROLES)[number];

/** `pending` | `approved` | `rejected` | `applied` | `expired`. */
export const CHANGE_REQUEST_STATUSES = adminChangeRequestSchema.shape.status.options;
export type ChangeRequestStatus = (typeof CHANGE_REQUEST_STATUSES)[number];

/** The admin mutation a change request applies when approved. */
export const CHANGE_REQUEST_KINDS = adminChangeRequestSchema.shape.kind.options;
export type ChangeRequestKind = (typeof CHANGE_REQUEST_KINDS)[number];

/** Why a change is guarded ("Requires a second admin" banner reasons). */
export const GUARD_REASON_CODES = adminChangeRequestSchema.shape.reasons.element.shape.code.options;
export type GuardReasonCode = (typeof GUARD_REASON_CODES)[number];

/** Per-op commit ledger states. */
export const COMMIT_LEDGER_STATES = commitLedgerEntrySchema.shape.state.options;
export type CommitLedgerState = (typeof COMMIT_LEDGER_STATES)[number];

/** `consistent` (tier-B snapshot) | `crawled` (tier-A crawl: plans are advisory). */
export const SNAPSHOT_CONSISTENCIES = scnSnapshotLabelSchema.shape.consistency.options;
export type SnapshotConsistency = (typeof SNAPSHOT_CONSISTENCIES)[number];

/** `change_set` | `per_op` | `guide`. */
export const COMMIT_MODES = connectorsCapabilitiesSchema.shape.commit.options;
export type CommitMode = (typeof COMMIT_MODES)[number];

/** The advisory label of plans built on crawled data (contracts-py `scn.CRAWLED_ADVISORY_TEXT`). */
export const CRAWLED_ADVISORY_TEXT = "Data gathered by paging, not a consistent snapshot";

/** Functions of the mapping expression language with their (min, max) arity (`mapping.EXPRESSION_FUNCTIONS`). */
export const MAPPING_EXPRESSION_FUNCTIONS = {
  int: [1, 1],
  str: [1, 1],
  date: [1, 2],
  lookup: [2, 3],
  coalesce: [2, 8],
  if_present: [3, 3],
} as const satisfies Record<string, readonly [number, number]>;

/** Change requests expire 72 h after they were made. */
export const CHANGE_REQUEST_TTL_MS = 72 * 60 * 60 * 1000;

/** A pending request that is past its expiry (the engine flips it to `expired`). */
export function changeRequestExpired(cr: Pick<AdminChangeRequest, "status" | "expires_at">, now: Date): boolean {
  return cr.status === "pending" && now.getTime() >= Date.parse(cr.expires_at);
}

/** Whether the viewer may press Approve: pending, not expired, not their own request (self-approval is impossible). */
export function canApproveChangeRequest(
  cr: Pick<AdminChangeRequest, "status" | "expires_at" | "requested_by">,
  viewerSub: string,
  now: Date,
): boolean {
  return cr.status === "pending" && !changeRequestExpired(cr, now) && cr.requested_by.sub !== viewerSub;
}

/** `pro` as its own word: a non-letter (separator, digit, space) or an end on both sides (contracts-py `agents._PRO_RE`). */
const PRO_RE = /(?:^|[^a-z])pro(?:$|[^a-z])/i;

/** True for an alias / model id naming a Pro tier (`gemini-2.5-pro`, `gemini-1.5-pro002`, `Gemini 2.5 Pro`; never allowed for custom specialists). */
export function isProModel(name: string): boolean {
  return PRO_RE.test(name);
}
