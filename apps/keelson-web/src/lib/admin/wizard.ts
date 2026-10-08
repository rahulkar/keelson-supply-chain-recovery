/**
 * "Add connector" wizard state machine (`70-backoffice-m2.md` §3, §6):
 *
 *   basics ──▶ auth ──(POST /connectors → draft)──▶ test ──(passing test)──▶ review ──▶ activate
 *
 * - leaving **auth** creates the draft connector; from then on basics/auth are read-only (there
 *   is no update route) and the typed secrets are dropped from memory;
 * - **review** is reachable only after a passing test; a failed test can be re-run;
 * - **activate** needs the last test to have passed (the server checks again).
 */
import type { StepLogItem } from "@keelson/ui";
import type { AuthKind, ConnectorTest, ConnectorTool } from "./types";

export const WIZARD_STEPS = ["basics", "auth", "test", "review", "activate"] as const;
export type WizardStep = (typeof WIZARD_STEPS)[number];

export const WIZARD_STEP_LABELS: Record<WizardStep, string> = {
  basics: "Basics",
  auth: "Authentication",
  test: "Test",
  review: "Review tools",
  activate: "Activate",
};

export type TrustLevel = "first_party" | "third_party";

export const TRUST_LEVEL_LABELS: Record<string, string> = { third_party: "Third party", first_party: "First party (your own system)" };

/** Registering a first-party connector (its tools are approved as suggested) needs a platform admin (engine 403). */
export function canRegisterFirstParty(user: { roles: readonly string[] }): boolean {
  return user.roles.includes("platform-admin");
}

export type Basics = { name: string; slug: string; slugTouched: boolean; mcp_url: string; trust_level: TrustLevel };

/** Kinds offered in the wizard (`oauth_platform_merxa` is platform-managed, never typed in). */
export type WizardAuthKind = Exclude<AuthKind, "oauth_platform_merxa">;

export type AuthForm = {
  kind: WizardAuthKind;
  header_name: string;
  secret: string;
  token_url: string;
  client_id: string;
  client_secret: string;
  scopes: string;
  resource: string;
};

export type TestState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; result: ConnectorTest }
  | { status: "error"; message: string };

export type WizardState = {
  step: WizardStep;
  basics: Basics;
  auth: AuthForm;
  /** Set once the draft exists on the server. */
  created: { slug: string } | null;
  busy: boolean;
  error: string | null;
  test: TestState;
  tools: ConnectorTool[];
  toolsLoaded: boolean;
  activated: boolean;
};

export const initialWizardState: WizardState = {
  step: "basics",
  basics: { name: "", slug: "", slugTouched: false, mcp_url: "", trust_level: "third_party" },
  auth: { kind: "static_header", header_name: "", secret: "", token_url: "", client_id: "", client_secret: "", scopes: "", resource: "" },
  created: null,
  busy: false,
  error: null,
  test: { status: "idle" },
  tools: [],
  toolsLoaded: false,
  activated: false,
};

export type WizardAction =
  | { type: "basics"; patch: Partial<Omit<Basics, "slugTouched">> }
  | { type: "auth"; patch: Partial<AuthForm> }
  | { type: "goto"; step: WizardStep }
  | { type: "busy"; busy: boolean }
  | { type: "error"; message: string | null }
  | { type: "created"; slug: string }
  | { type: "test_start" }
  | { type: "test_done"; result: ConnectorTest }
  | { type: "test_error"; message: string }
  | { type: "tools_loaded"; tools: ConnectorTool[] }
  | { type: "tool_updated"; tool: ConnectorTool }
  | { type: "activated" };

// ───────────────────────────── validation ─────────────────────────────

export const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{0,62}$/;
/** Header names the engine accepts for `static_header` (`connector_admin.HEADER_RE`). */
const HEADER_NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,63}$/;
/** Headers the engine refuses to let a connector set (transport / session / proxy headers). */
export const RESERVED_HEADERS: ReadonlySet<string> = new Set([
  "host",
  "cookie",
  "content-length",
  "content-type",
  "transfer-encoding",
  "connection",
  "upgrade",
  "proxy-authorization",
  "mcp-session-id",
  "mcp-protocol-version",
  "accept",
  "te",
  "trailer",
]);
/** Longest secret the engine stores (`SECRET_MAX`). */
export const SECRET_MAX_CHARS = 4096;

export function slugify(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63)
    .replace(/-+$/g, "");
}

function httpUrl(raw: string): URL | null {
  try {
    const u = new URL(raw.trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u : null;
  } catch {
    return null;
  }
}

export type FieldErrors = Partial<Record<string, string>>;

export function validateBasics(b: Basics): FieldErrors {
  const e: FieldErrors = {};
  if (!b.name.trim()) e["name"] = "Give the connector a name.";
  if (!SLUG_PATTERN.test(b.slug)) e["slug"] = "Lower-case letters, digits and hyphens; starts with a letter or digit.";
  const u = b.mcp_url.trim();
  if (!u) e["mcp_url"] = "Enter the MCP server URL.";
  else {
    const url = httpUrl(u);
    if (!url) e["mcp_url"] = "Use an http:// or https:// URL.";
    else if (url.username || url.password) e["mcp_url"] = "Don't put credentials in the URL; use Authentication.";
  }
  return e;
}

export function validateAuth(a: AuthForm): FieldErrors {
  const e: FieldErrors = {};
  if (a.kind === "static_header") {
    const header = a.header_name.trim();
    if (!header) e["header_name"] = "Enter the header name, e.g. X-Api-Key.";
    else if (!HEADER_NAME.test(header)) e["header_name"] = "Letters, digits and - only (up to 64), starting with a letter or digit.";
    else if (RESERVED_HEADERS.has(header.toLowerCase())) e["header_name"] = `${header} is reserved; use the header your server reads the key from.`;
    if (!a.secret.trim()) e["secret"] = "Enter the header value.";
    else if (/[\r\n]/.test(a.secret)) e["secret"] = "The header value must be on one line.";
    else if (a.secret.length > SECRET_MAX_CHARS) e["secret"] = `At most ${SECRET_MAX_CHARS.toLocaleString("en-GB")} characters.`;
  } else if (a.kind === "oauth_client_credentials") {
    if (!a.token_url.trim()) e["token_url"] = "Enter the token endpoint.";
    else if (!httpUrl(a.token_url)) e["token_url"] = "Use an http:// or https:// URL.";
    if (!a.client_id.trim()) e["client_id"] = "Enter the client ID.";
    else if (a.client_id.trim().length > 256) e["client_id"] = "At most 256 characters.";
    if (!a.client_secret) e["client_secret"] = "Enter the client secret.";
    else if (a.client_secret.length > SECRET_MAX_CHARS) e["client_secret"] = `At most ${SECRET_MAX_CHARS.toLocaleString("en-GB")} characters.`;
    if (a.resource.trim() && !httpUrl(a.resource)) e["resource"] = "Use an http:// or https:// URL.";
  }
  return e;
}

/** The `auth` body of `POST /api/admin/connectors` (only the fields of the chosen kind). */
export function authPayload(a: AuthForm) {
  switch (a.kind) {
    case "none":
      return { kind: a.kind };
    case "static_header":
      return { kind: a.kind, header_name: a.header_name.trim(), secret: a.secret };
    case "oauth_client_credentials": {
      const scopes = a.scopes.split(/[\s,]+/).filter(Boolean);
      return {
        kind: a.kind,
        token_url: a.token_url.trim(),
        client_id: a.client_id.trim(),
        client_secret: a.client_secret,
        ...(scopes.length ? { scopes } : {}),
        ...(a.resource.trim() ? { resource: a.resource.trim() } : {}),
      };
    }
  }
}

export function createPayload(s: WizardState) {
  return {
    slug: s.basics.slug,
    name: s.basics.name.trim(),
    mcp_url: s.basics.mcp_url.trim(),
    trust_level: s.basics.trust_level,
    auth: authPayload(s.auth),
  };
}

// ───────────────────────────── navigation ─────────────────────────────

export function testPassed(s: WizardState): boolean {
  return s.test.status === "done" && s.test.result.ok;
}

/** Whether `step` may be shown now (forward moves need everything before it done). */
export function canEnter(s: WizardState, step: WizardStep): boolean {
  switch (step) {
    case "basics":
    case "auth":
      return true;
    case "test":
      return s.created !== null;
    case "review":
    case "activate":
      return s.created !== null && testPassed(s);
  }
}

/** Tools still waiting for a decision (highlighted, never served to agents). */
export function toolsNeedingReview(s: Pick<WizardState, "tools">): ConnectorTool[] {
  return s.tools.filter((t) => t.status === "needs_review");
}

export function wizardReducer(state: WizardState, action: WizardAction): WizardState {
  switch (action.type) {
    case "basics": {
      if (state.created) return state; // the draft exists: read-only
      const basics = { ...state.basics, ...action.patch };
      if (action.patch.slug !== undefined) basics.slugTouched = true;
      else if (action.patch.name !== undefined && !state.basics.slugTouched) basics.slug = slugify(action.patch.name);
      return { ...state, basics, error: null };
    }
    case "auth":
      if (state.created) return state;
      return { ...state, auth: { ...state.auth, ...action.patch }, error: null };
    case "goto":
      return canEnter(state, action.step) ? { ...state, step: action.step, error: null } : state;
    case "busy":
      return { ...state, busy: action.busy };
    case "error":
      return { ...state, error: action.message, busy: false };
    case "created":
      // Secrets are on the server now (encrypted); never keep them in the page.
      return {
        ...state,
        created: { slug: action.slug },
        auth: { ...state.auth, secret: "", client_secret: "" },
        busy: false,
        error: null,
        step: "test",
      };
    case "test_start":
      return state.created ? { ...state, test: { status: "running" }, toolsLoaded: false, error: null } : state;
    case "test_done":
      return { ...state, test: { status: "done", result: action.result } };
    case "test_error":
      return { ...state, test: { status: "error", message: action.message } };
    case "tools_loaded":
      return { ...state, tools: action.tools, toolsLoaded: true };
    case "tool_updated":
      return { ...state, tools: state.tools.map((t) => (t.name === action.tool.name ? action.tool : t)) };
    case "activated":
      return { ...state, activated: true, busy: false, error: null };
  }
}

/** Test step ids → the step log (pending ones are shown for steps the server has not reported). */
export const TEST_STEP_LABELS: Record<string, string> = {
  url_policy: "URL policy",
  reach: "Reach the server (via egress proxy)",
  discover: "Discover server",
  auth: "Authenticate",
  tools_list: "List tools",
  tier_detect: "Detect capability tier",
  classify: "Classify tools",
};

function stepDetail(detail: unknown): string | undefined {
  if (detail === undefined || detail === null || detail === "") return undefined;
  if (typeof detail === "string") return detail;
  if (typeof detail === "number" || typeof detail === "boolean") return String(detail);
  try {
    const s = JSON.stringify(detail);
    return s.length > 300 ? `${s.slice(0, 299)}…` : s;
  } catch {
    return undefined;
  }
}

function humanStep(id: string): string {
  return (Object.hasOwn(TEST_STEP_LABELS, id) ? TEST_STEP_LABELS[id] : undefined) ?? id.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/**
 * The step log for a test: every known step (in order) plus any extra the server reports.
 * While the single `POST …/test` call runs, the first step shows as running; once it answers,
 * reported steps carry their result and known steps it never reached are "skipped".
 */
export function testLogItems(test: TestState): StepLogItem[] {
  const known = Object.keys(TEST_STEP_LABELS);
  if (test.status === "idle" || test.status === "error") {
    return known.map((id) => ({ id, label: humanStep(id), state: "pending" as const }));
  }
  if (test.status === "running") {
    return known.map((id, i) => ({ id, label: humanStep(id), state: i === 0 ? ("running" as const) : ("pending" as const) }));
  }
  const reported = new Map(test.result.steps.map((s) => [s.step, s]));
  const failedAt = test.result.steps.findIndex((s) => !s.ok);
  const order = [...known, ...test.result.steps.map((s) => s.step).filter((id) => !known.includes(id))];
  return order.map((id) => {
    const s = reported.get(id);
    if (s) return { id, label: humanStep(id), state: s.ok ? ("ok" as const) : ("failed" as const), ms: s.ms ?? null, detail: stepDetail(s.detail) };
    return { id, label: humanStep(id), state: failedAt >= 0 || !test.result.ok ? ("skipped" as const) : ("pending" as const) };
  });
}
