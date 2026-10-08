/**
 * Browser API client for engine-api (same origin, `/api/*` routed by Caddy).
 *
 * - `credentials: "include"` so the HttpOnly `keelson_session` cookie travels;
 * - every mutation carries `X-CSRF-Token` (from `GET /api/me`); a `403 csrf_failed` refreshes the
 *   token once and retries (the session may have been replaced, e.g. by a persona switch);
 * - `POST …/messages` and `…/actions` carry an `Idempotency-Key` chosen by the caller, so a retry
 *   of the same submission can never start a second run;
 * - `401` hands control to `onUnauthorized` (default: go to the BFF sign-in, returning here).
 */
import { z } from "zod";
import { loginUrl } from "../paths";
import {
  apiErrorBodySchema,
  caseDetailSchema,
  caseListSchema,
  connectorLinkSchema,
  createdCaseSchema,
  decisionResultSchema,
  logoutResultSchema,
  meSchema,
  personasSchema,
  runAcceptedSchema,
  checklistStepResultSchema,
  demoResetResultSchema,
  type CaseDetail,
  type ChecklistStepResult,
  type DemoResetResult,
  type CaseSummary,
  type ConnectorLink,
  type DecisionResult,
  type Me,
  type Persona,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly body: Record<string, unknown>;

  constructor(status: number, code: string, message: string, body: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

export function isApiError(err: unknown, code?: string): err is ApiError {
  return err instanceof ApiError && (code === undefined || err.code === code);
}

/** A short, human sentence for a failed call (toasts). */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case "network_error":
        return "Couldn't reach Keelson. Check your connection and try again.";
      case "forbidden":
        return err.message ? capitalise(err.message) : "Your role doesn't allow this.";
      case "stale":
      case "stale_plan":
        return "This card is out of date. The latest version is shown.";
      case "superseded":
        return "This request was replaced by a newer one.";
      case "csrf_failed":
      case "bad_origin":
        return "Your session changed. Reload the page and try again.";
      default:
        return err.message ? capitalise(err.message) : "Something went wrong.";
    }
  }
  return "Something went wrong.";
}

function capitalise(s: string): string {
  const t = s.trim();
  const sentence = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(sentence) ? sentence : `${sentence}.`;
}

export type ApiClientOptions = {
  fetch?: typeof fetch | undefined;
  /** Called on 401 (after which the request still rejects). */
  onUnauthorized?: (() => void) | undefined;
  /** A CSRF token already known (e.g. server-rendered `/api/me`). */
  csrfToken?: string | null | undefined;
};

/**
 * `POST /api/cases/{id}/actions` (contracts `ActionPayload`): `confirm_assumptions` (M4) names
 * the values it confirms in `assumption_keys` (at most 50).
 */
export type CaseActionBody =
  | {
      type: "apply" | "guide" | "promote" | "confirm_assumptions";
      option_id: string;
      plan_version: number;
      assumption_keys?: string[] | undefined;
    }
  /** M5 commit tracker **Undo changes** (`docs/contracts/95` §3): only the applied change set's id. */
  | { type: "rollback"; change_set_id: string };

/** A typed approach-card edit (contracts `ApproachEdit`). */
export type ApproachEditBody = { key: string; value_int?: number | undefined; value_ids?: string[] | undefined };

/**
 * `POST /api/interrupts/{id}/decision`. `edits` is the M1 free text or (M4) a list of typed
 * edits; `removed_checks` are approach-card task ids (M4).
 */
export type DecisionBody = {
  decision: string;
  plan_version?: number | undefined;
  change_set_hash?: string | undefined;
  edits?: string | ApproachEditBody[] | undefined;
  removed_checks?: string[] | undefined;
  /** M5: the decider's note on `reject` / `revise` (1–500 characters). */
  reason?: string | undefined;
};

export type HttpMethod ="GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export type RequestOptions<S extends z.ZodType> = {
  method?: HttpMethod;
  body?: unknown;
  schema: S;
  idempotencyKey?: string | undefined;
  /** Mutations need the CSRF token; GETs don't. Default: method !== GET. */
  csrf?: boolean | undefined;
  /** Aborts the request (e.g. a block's payload is no longer shown). */
  signal?: AbortSignal | undefined;
  /** A `multipart/form-data` body (uploads) instead of JSON `body`; the browser sets the boundary. */
  form?: FormData | undefined;
};

function defaultUnauthorized(): void {
  if (typeof window === "undefined") return;
  const { pathname, search, hash } = window.location;
  window.location.assign(loginUrl(`${pathname}${search}${hash}`));
}

/** Same-origin data endpoint of a block (the only `data_ref` the engine may emit). */
export function blockDataPath(blockId: string): string {
  // encodeURIComponent leaves "." alone, and the URL parser resolves "." / ".." (even as %2E)
  // as dot segments: "/api/blocks/../data" would request "/api/data". Such ids are refused.
  if (blockId === "" || blockId === "." || blockId === "..") throw new Error(`Invalid block id "${blockId}"`);
  return `/api/blocks/${encodeURIComponent(blockId)}/data`;
}

export class ApiClient {
  private readonly fetchImpl: typeof fetch;
  private readonly onUnauthorized: () => void;
  private csrf: string | null;
  private csrfInflight: Promise<string> | null = null;

  constructor(opts: ApiClientOptions = {}) {
    this.fetchImpl = opts.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.onUnauthorized = opts.onUnauthorized ?? defaultUnauthorized;
    this.csrf = opts.csrfToken ?? null;
  }

  setCsrfToken(token: string | null): void {
    this.csrf = token;
  }

  // ───────────────────────────── plumbing ─────────────────────────────

  private async csrfToken(force = false): Promise<string> {
    if (this.csrf && !force) return this.csrf;
    this.csrfInflight ??= this.me()
      .then((me) => me.csrf_token)
      .finally(() => {
        this.csrfInflight = null;
      });
    return this.csrfInflight;
  }

  private async send(path: string, opts: RequestOptions<z.ZodType>, csrf: string | null): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (opts.body !== undefined && !opts.form) headers["Content-Type"] = "application/json";
    if (csrf) headers["X-CSRF-Token"] = csrf;
    if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
    try {
      return await this.fetchImpl(path, {
        method: opts.method ?? "GET",
        credentials: "include",
        cache: "no-store",
        headers,
        ...(opts.form ? { body: opts.form } : opts.body !== undefined ? { body: JSON.stringify(opts.body) } : {}),
        ...(opts.signal ? { signal: opts.signal } : {}),
      });
    } catch (err) {
      if (opts.signal?.aborted) throw err;
      throw new ApiError(0, "network_error", err instanceof Error ? err.message : "network error");
    }
  }

  private async request<S extends z.ZodType>(path: string, opts: RequestOptions<S>): Promise<z.infer<S>> {
    return (await this.requestWithStatus(path, opts)).data;
  }

  private async requestWithStatus<S extends z.ZodType>(path: string, opts: RequestOptions<S>): Promise<{ status: number; data: z.infer<S> }> {
    const method = opts.method ?? "GET";
    const needsCsrf = opts.csrf ?? method !== "GET";
    let res = await this.send(path, opts, needsCsrf ? await this.csrfToken() : null);
    if (needsCsrf && res.status === 403) {
      const err = await errorFrom(res.clone());
      if (err.code === "csrf_failed") res = await this.send(path, opts, await this.csrfToken(true));
    }
    if (res.status === 401) {
      this.onUnauthorized();
      throw await errorFrom(res);
    }
    if (!res.ok) throw await errorFrom(res);
    const json: unknown = res.status === 204 ? {} : await res.json().catch(() => {
      throw new ApiError(res.status, "invalid_response", "The server sent an unreadable response.");
    });
    const parsed = opts.schema.safeParse(json);
    if (!parsed.success) throw new ApiError(res.status, "invalid_response", "The server sent an unexpected response.");
    return { status: res.status, data: parsed.data };
  }

  /**
   * Typed call for feature clients built on this one (e.g. `AdminApi`): same CSRF, 401 and
   * error handling as every built-in call; the response is validated with `schema`.
   */
  call<S extends z.ZodType>(path: string, opts: RequestOptions<S>): Promise<z.infer<S>> {
    return this.request(path, opts);
  }

  /**
   * Like `call`, but `schemaFor(status)` picks the schema by the HTTP status (e.g. a guarded
   * admin change answered `202` with a change request instead of the applied value).
   */
  async callByStatus<T>(path: string, opts: Omit<RequestOptions<z.ZodType>, "schema">, schemaFor: (status: number) => z.ZodType<T>): Promise<{ status: number; data: T }> {
    const raw = await this.requestWithStatus(path, { ...opts, schema: z.unknown() });
    const parsed = schemaFor(raw.status).safeParse(raw.data);
    if (!parsed.success) throw new ApiError(raw.status, "invalid_response", "The server sent an unexpected response.");
    return { status: raw.status, data: parsed.data };
  }

  /**
   * POST that answers a stream (`text/event-stream`, e.g. the admin agent playground): same
   * CSRF / 401 / error handling as `call`; `onFrame` gets each SSE `data:` payload (JSON-parsed,
   * unparseable frames skipped). A JSON answer is returned as `json` instead (non-streaming server).
   */
  async postStream(
    path: string,
    body: unknown,
    onFrame: (frame: unknown, event: string | null) => void,
    opts: { signal?: AbortSignal | undefined } = {},
  ): Promise<{ streamed: boolean; json: unknown }> {
    const csrf = await this.csrfToken();
    const send = (token: string) =>
      this.fetchImpl(path, {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: { Accept: "text/event-stream, application/json", "Content-Type": "application/json", "X-CSRF-Token": token },
        body: JSON.stringify(body),
        ...(opts.signal ? { signal: opts.signal } : {}),
      });
    let res: Response;
    try {
      res = await send(csrf);
      if (res.status === 403 && (await errorFrom(res.clone())).code === "csrf_failed") res = await send(await this.csrfToken(true));
    } catch (err) {
      if (opts.signal?.aborted) throw err;
      throw new ApiError(0, "network_error", err instanceof Error ? err.message : "network error");
    }
    if (res.status === 401) {
      this.onUnauthorized();
      throw await errorFrom(res);
    }
    if (!res.ok) throw await errorFrom(res);
    const type = res.headers.get("Content-Type") ?? "";
    if (!type.includes("text/event-stream") || !res.body) {
      const json: unknown = await res.json().catch(() => {
        throw new ApiError(res.status, "invalid_response", "The server sent an unreadable response.");
      });
      return { streamed: false, json };
    }
    await readSse(res.body, onFrame);
    return { streamed: true, json: null };
  }

  // ───────────────────────────── auth ─────────────────────────────

  async me(): Promise<Me> {
    const me = await this.request("/api/me", { schema: meSchema });
    this.csrf = me.csrf_token;
    return me;
  }

  personas(): Promise<Persona[]> {
    return this.request("/api/demo/personas", { schema: personasSchema });
  }

  /** Switch persona (DEMO). The response sets a new session cookie; callers reload the page. */
  async loginAs(username: string): Promise<void> {
    await this.request("/api/demo/login-as", { method: "POST", body: { username }, schema: z.object({ ok: z.boolean() }).loose() });
    this.csrf = null;
  }

  /** End the BFF session; returns the IdP end-session URL to visit (if any). */
  async logout(): Promise<string | null> {
    const out = await this.request("/api/auth/logout", { method: "POST", schema: logoutResultSchema });
    this.csrf = null;
    return out.logout_url ?? null;
  }

  // ───────────────────────────── cases ─────────────────────────────

  async listCases(limit = 50): Promise<CaseSummary[]> {
    const out = await this.request(`/api/cases?limit=${encodeURIComponent(String(limit))}`, { schema: caseListSchema });
    return out.items;
  }

  getCase(caseId: string): Promise<CaseDetail> {
    return this.request(`/api/cases/${encodeURIComponent(caseId)}`, { schema: caseDetailSchema });
  }

  async createCase(title?: string): Promise<string> {
    const out = await this.request("/api/cases", {
      method: "POST",
      body: title ? { title } : {},
      schema: createdCaseSchema,
    });
    return out.id;
  }

  async postMessage(caseId: string, text: string, idempotencyKey: string): Promise<string> {
    const out = await this.request(`/api/cases/${encodeURIComponent(caseId)}/messages`, {
      method: "POST",
      body: { text },
      idempotencyKey,
      schema: runAcceptedSchema,
    });
    return out.run_id;
  }

  async postAction(
    caseId: string,
    action: CaseActionBody,
    idempotencyKey: string,
  ): Promise<string> {
    const out = await this.request(`/api/cases/${encodeURIComponent(caseId)}/actions`, {
      method: "POST",
      body: action,
      idempotencyKey,
      schema: runAcceptedSchema,
    });
    return out.run_id;
  }

  decide(
    interruptId: string,
    body: DecisionBody,
  ): Promise<DecisionResult> {
    return this.request(`/api/interrupts/${encodeURIComponent(interruptId)}/decision`, {
      method: "POST",
      body,
      schema: decisionResultSchema,
    });
  }

  /**
   * M5 Guide mode: a person marks a checklist step `done` or `skipped`
   * (`POST /api/cases/{id}/checklists/{checklist_id}/steps/{seq}`, audited). `verified` is set only
   * by the engine's read-back (`409 already_verified` for a verified step). The refreshed block
   * arrives on the case stream.
   */
  tickChecklistStep(caseId: string, checklistId: string, seq: number, status: "done" | "skipped"): Promise<ChecklistStepResult> {
    if (!Number.isInteger(seq) || seq < 1) return Promise.reject(new ApiError(0, "invalid_request", "Invalid step."));
    // A person may only mark a step done or skipped; "verified" comes from the engine's read-back.
    if (status !== "done" && status !== "skipped") return Promise.reject(new ApiError(0, "invalid_request", "Invalid step status."));
    return this.request(
      `/api/cases/${encodeURIComponent(caseId)}/checklists/${encodeURIComponent(checklistId)}/steps/${encodeURIComponent(String(seq))}`,
      { method: "POST", body: { status }, schema: checklistStepResultSchema },
    );
  }

  /**
   * M5 full demo reset (`POST /api/demo/reset`, DEMO_MODE only, platform-admin or
   * demo-presenter). Answers the step log, also when a step failed (`ok: false`);
   * `409 reset_in_progress` while another reset runs.
   */
  demoReset(keepConsents = false): Promise<DemoResetResult> {
    return this.request("/api/demo/reset", { method: "POST", body: { keep_consents: keepConsents }, schema: demoResetResultSchema });
  }

  async cancelRun(runId: string): Promise<void> {
    await this.request(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      schema: z.object({ status: z.string().optional() }).loose(),
    });
  }

  /** The current user's link to a connector (whether they could approve changes right now). */
  connectorLink(slug: "merxa"): Promise<ConnectorLink> {
    return this.request(`/api/connectors/${encodeURIComponent(slug)}/link`, { schema: connectorLinkSchema });
  }

  /**
   * Large block payload referenced by `data_ref` (`GET /api/blocks/{id}/data`): always the
   * block's own endpoint, never a URL taken from the envelope. Validated per kind by the
   * renderer (`parseBlockData`).
   */
  blockData(blockId: string, opts: { signal?: AbortSignal | undefined } = {}): Promise<unknown> {
    let path: string;
    try {
      path = blockDataPath(blockId);
    } catch (err) {
      return Promise.reject(err instanceof Error ? err : new Error(String(err)));
    }
    return this.request(path, { schema: z.unknown(), signal: opts.signal });
  }
}

/** Reads an SSE body: frames separated by a blank line; `data:` lines joined; `event:` names kept. */
export async function readSse(body: ReadableStream<Uint8Array>, onFrame: (frame: unknown, event: string | null) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flush = (block: string) => {
    let event: string | null = null;
    const data: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      else if (line.startsWith("event:")) event = line.slice(6).trim();
    }
    if (!data.length) return;
    try {
      onFrame(JSON.parse(data.join("\n")) as unknown, event);
    } catch {
      // not JSON (a comment / keep-alive): skipped
    }
  };
  const boundary = /\r?\n\r?\n/;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let match: RegExpExecArray | null;
    while ((match = boundary.exec(buffer)) !== null) {
      const block = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      flush(block);
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) flush(buffer);
}

async function errorFrom(res: Response): Promise<ApiError> {
  let body: Record<string, unknown> = {};
  try {
    const json: unknown = await res.json();
    if (json && typeof json === "object" && !Array.isArray(json)) body = json as Record<string, unknown>;
  } catch {
    // non-JSON error (proxy page, empty body)
  }
  const parsed = apiErrorBodySchema.safeParse(body);
  const code = parsed.success ? parsed.data.code : res.status === 401 ? "unauthenticated" : `http_${res.status}`;
  const message = parsed.success && parsed.data.message ? parsed.data.message : res.statusText || "request failed";
  return new ApiError(res.status, code, message, body);
}

/** The app-wide client (browser). Components get it through `useApi()`. */
export const api = new ApiClient();
