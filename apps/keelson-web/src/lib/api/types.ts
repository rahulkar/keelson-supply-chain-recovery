/**
 * Response shapes of the engine-api routes this app calls (`30-engine-api-events.md`, `10-auth.md`).
 * Parsed with Zod at the boundary; objects are `loose` so additive server fields never break the UI.
 */
import { z } from "zod";

export const meUserSchema = z
  .object({
    sub: z.string(),
    username: z.string(),
    name: z.string(),
    email: z.string().nullable().optional(),
    roles: z.array(z.string()),
    tenants: z.array(z.string()),
    active_tenant: z.string().nullable(),
  })
  .loose();
export type MeUser = z.infer<typeof meUserSchema>;

export const meSchema = z
  .object({
    user: meUserSchema,
    csrf_token: z.string().min(1),
    demo_mode: z.boolean(),
    brand: z.object({ engine: z.string(), vendor: z.string() }).loose(),
  })
  .loose();
export type Me = z.infer<typeof meSchema>;

export const personaSchema = z
  .object({ username: z.string(), name: z.string(), role_label: z.string(), initials: z.string() })
  .loose();
export const personasSchema = z.array(personaSchema);
export type Persona = z.infer<typeof personaSchema>;

export const caseSummarySchema = z
  .object({
    id: z.string(),
    title: z.string(),
    mode: z.string(),
    status: z.string(),
    updated_at: z.string().nullable(),
    last_message_preview: z.string().nullable(),
  })
  .loose();
export const caseListSchema = z.object({ items: z.array(caseSummarySchema) }).loose();
export type CaseSummary = z.infer<typeof caseSummarySchema>;

export const caseMessageSchema = z
  .object({
    id: z.union([z.string(), z.number()]).transform(String),
    role: z.string(),
    text: z.string(),
    run_id: z.string().nullable(),
    created_at: z.string().nullable(),
  })
  .loose();
export type CaseMessage = z.infer<typeof caseMessageSchema>;

export const openInterruptSchema = z
  .object({
    id: z.string(),
    kind: z.string(),
    message: z.string().nullable(),
    payload: z.unknown(),
    response_schema: z.unknown(),
    /** M5: approval progress (`QuorumView`) of a quorum interrupt. */
    quorum: z.unknown().optional(),
  })
  .loose();
export type OpenInterrupt = z.infer<typeof openInterruptSchema>;

/**
 * A resolved interrupt with who decided it (optional, forward-compatible: engine-api does not
 * send `decided_interrupts` yet; without it the card shows what this page knows).
 */
export const decidedInterruptSchema = z
  .object({
    id: z.string(),
    decision: z.string().nullable().optional(),
    status: z.string().nullable().optional(),
    decided_by: z
      .array(z.union([z.string(), z.object({ name: z.string().nullable().optional(), username: z.string().nullable().optional() }).loose()]))
      .optional(),
    decided_at: z.string().nullable().optional(),
    resolved_at: z.string().nullable().optional(),
  })
  .loose();
export type DecidedInterrupt = z.infer<typeof decidedInterruptSchema>;

export const caseDetailSchema = z
  .object({
    decided_interrupts: z.array(decidedInterruptSchema).optional().catch(undefined),
    case: z
      .object({
        id: z.string(),
        title: z.string(),
        mode: z.string(),
        status: z.string(),
        created_by: z.unknown(),
        created_at: z.string().nullable(),
      })
      .loose(),
    messages: z.array(caseMessageSchema),
    blocks: z.array(z.unknown()),
    open_interrupts: z.array(openInterruptSchema),
    last_event_seq: z.number().int().nonnegative(),
    /** Queued runs no user message announces (M6: the `signal` run of a case opened from an alert). */
    pending_runs: z
      .array(z.object({ id: z.string().min(1), kind: z.string() }).loose())
      .optional()
      .catch(undefined),
  })
  .loose();
export type CaseDetail = z.infer<typeof caseDetailSchema>;

export const createdCaseSchema = z.object({ id: z.string().min(1) }).loose();
export const runAcceptedSchema = z.object({ run_id: z.string().min(1) }).loose();
export const decisionResultSchema = z
  .object({ status: z.enum(["recorded", "resumed"]), run_id: z.string().nullable().optional() })
  .loose();
export type DecisionResult = z.infer<typeof decisionResultSchema>;

/**
 * `GET /api/connectors/{slug}/link` for the **current user** (`services/engine/README.md` →
 * Connect-Merxa). `apply_ready` (`status == "active"` and `changes:apply` granted) is the exact
 * check behind the decision API's `412 connect_required`; derived from status + scopes for an
 * engine that does not send it yet. Unknown statuses read as `missing`.
 */
export const connectorLinkSchema = z
  .object({
    status: z.string(),
    ext_username: z.string().nullable().optional(),
    roles: z.array(z.string()).optional(),
    scopes: z.array(z.string()).optional(),
    apply_ready: z.boolean().optional(),
  })
  .loose()
  .transform((link) => {
    const status = link.status === "active" || link.status === "expired" ? link.status : "missing";
    const applyReady = link.apply_ready ?? (status === "active" && (link.scopes ?? []).includes("changes:apply"));
    return { status, applyReady: status === "active" && applyReady, extUsername: link.ext_username ?? null } as const;
  });
export type ConnectorLink = z.infer<typeof connectorLinkSchema>;

/** `POST /api/cases/{id}/checklists/{cid}/steps/{seq}` (M5): the step as stored + whether it changed. */
export const checklistStepResultSchema = z
  .object({
    step: z.object({ seq: z.number().int(), status: z.string() }).loose(),
    changed: z.boolean().optional(),
  })
  .loose();
export type ChecklistStepResult = z.infer<typeof checklistStepResultSchema>;

/**
 * `POST /api/demo/reset` (M5, contracts `DemoResetResult`): the step log. Loose so additive
 * server fields never break the toast; `steps` keep what the person needs (name, status, time,
 * detail, counts).
 */
export const demoResetStepSchema = z
  .object({
    step: z.string(),
    status: z.enum(["ok", "skipped", "failed"]).catch("failed"),
    duration_ms: z.number().nonnegative().catch(0),
    detail: z.string().nullable().optional(),
    counts: z.record(z.string(), z.number()).optional().catch(undefined),
  })
  .loose();
export const demoResetResultSchema = z
  .object({
    ok: z.boolean(),
    keep_consents: z.boolean().optional(),
    started_at: z.string().optional(),
    duration_ms: z.number().nonnegative().catch(0),
    steps: z.array(demoResetStepSchema),
  })
  .loose();
export type DemoResetResult = z.infer<typeof demoResetResultSchema>;
export type DemoResetStep = z.infer<typeof demoResetStepSchema>;

export const logoutResultSchema = z.object({ ok: z.boolean(), logout_url: z.string().nullable().optional() }).loose();

/** `{"code": "...", "message": "...", ...extra}` error body of every engine-api route. */
export const apiErrorBodySchema = z
  .object({ code: z.string(), message: z.string().optional() })
  .loose();
export type ApiErrorBody = z.infer<typeof apiErrorBodySchema>;
