import { describe, expect, it } from "vitest";
import type { ConnectorTest } from "./types";
import {
  authPayload,
  canEnter,
  createPayload,
  initialWizardState,
  slugify,
  testLogItems,
  toolsNeedingReview,
  validateAuth,
  validateBasics,
  wizardReducer,
  type WizardAction,
  type WizardState,
} from "./wizard";

const run = (actions: WizardAction[], start: WizardState = initialWizardState) => actions.reduce(wizardReducer, start);

const passed: ConnectorTest = {
  ok: true,
  tier: ["A"],
  steps: [
    { step: "url_policy", ok: true, ms: 1 },
    { step: "reach", ok: true, ms: 10 },
  ],
  tools: [],
};
const failed: ConnectorTest = {
  ok: false,
  tier: [],
  steps: [
    { step: "url_policy", ok: true, ms: 1 },
    { step: "reach", ok: false, ms: 3000, detail: { error: "connection refused" } },
  ],
  tools: [],
};

const filled = run([
  { type: "basics", patch: { name: "ERP Lite (test)" } },
  { type: "basics", patch: { mcp_url: "http://merxa.localhost/mcp-lite" } },
  { type: "auth", patch: { kind: "static_header", header_name: "X-Merxa-Api-Key", secret: "s3cret" } },
]);

describe("connector wizard state machine", () => {
  it("derives the slug from the name until the slug is edited", () => {
    expect(filled.basics.slug).toBe("erp-lite-test");
    const edited = run([{ type: "basics", patch: { slug: "lite" } }, { type: "basics", patch: { name: "Other" } }], filled);
    expect(edited.basics.slug).toBe("lite");
    expect(slugify("  Ünïcode — ERP!! ")).toBe("unicode-erp");
  });

  it("cannot skip ahead: test needs a draft, review/activate need a passing test", () => {
    expect(canEnter(filled, "auth")).toBe(true);
    expect(wizardReducer(filled, { type: "goto", step: "test" })).toBe(filled);
    expect(wizardReducer(filled, { type: "goto", step: "review" })).toBe(filled);
    const created = wizardReducer(filled, { type: "created", slug: "erp-lite-test" });
    expect(created.step).toBe("test");
    expect(canEnter(created, "review")).toBe(false);
    const failing = run([{ type: "test_start" }, { type: "test_done", result: failed }], created);
    expect(wizardReducer(failing, { type: "goto", step: "review" }).step).toBe("test");
    const passing = run([{ type: "test_start" }, { type: "test_done", result: passed }], failing);
    expect(wizardReducer(passing, { type: "goto", step: "review" }).step).toBe("review");
    expect(wizardReducer(passing, { type: "goto", step: "activate" }).step).toBe("activate");
    // re-running the test locks review again until it passes
    const rerun = wizardReducer(passing, { type: "test_start" });
    expect(canEnter(rerun, "review")).toBe(false);
  });

  it("drops typed secrets once the draft exists and locks basics/auth", () => {
    const created = wizardReducer(filled, { type: "created", slug: "erp-lite-test" });
    expect(created.auth.secret).toBe("");
    expect(created.auth.client_secret).toBe("");
    expect(created.auth.header_name).toBe("X-Merxa-Api-Key");
    expect(wizardReducer(created, { type: "basics", patch: { name: "changed" } })).toBe(created);
    expect(wizardReducer(created, { type: "auth", patch: { secret: "again" } })).toBe(created);
    // going back is allowed (read-only view)
    expect(wizardReducer(created, { type: "goto", step: "basics" }).step).toBe("basics");
  });

  it("tracks tools and those still needing review", () => {
    const s = run([
      { type: "created", slug: "x" },
      {
        type: "tools_loaded",
        tools: [
          { name: "a", access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none", status: "needs_review" },
          { name: "b", access: "write", content_trust: "trusted_data", egress: "internal", idempotency: "none", status: "needs_review" },
        ],
      },
      { type: "tool_updated", tool: { name: "a", access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none", status: "approved" } },
    ]);
    expect(s.toolsLoaded).toBe(true);
    expect(toolsNeedingReview(s).map((t) => t.name)).toEqual(["b"]);
  });

  it("an error clears busy; activation is recorded", () => {
    const s = run([{ type: "busy", busy: true }, { type: "error", message: "nope" }]);
    expect(s).toMatchObject({ busy: false, error: "nope" });
    expect(run([{ type: "busy", busy: true }, { type: "activated" }])).toMatchObject({ busy: false, activated: true });
  });
});

describe("validation", () => {
  it("basics: name, slug pattern, http(s) URL without credentials", () => {
    expect(validateBasics(filled.basics)).toEqual({});
    const bad = validateBasics({ name: " ", slug: "-Bad", slugTouched: true, mcp_url: "ftp://x", trust_level: "third_party" });
    expect(Object.keys(bad).sort()).toEqual(["mcp_url", "name", "slug"]);
    expect(validateBasics({ ...filled.basics, mcp_url: "https://user:pw@erp.example.com/mcp" })["mcp_url"]).toMatch(/credentials/);
  });

  it("auth: required fields per kind", () => {
    expect(validateAuth(filled.auth)).toEqual({});
    expect(Object.keys(validateAuth({ ...filled.auth, header_name: "bad header", secret: "" })).sort()).toEqual(["header_name", "secret"]);
    const oauth = { ...filled.auth, kind: "oauth_client_credentials" as const };
    expect(Object.keys(validateAuth(oauth)).sort()).toEqual(["client_id", "client_secret", "token_url"]);
    expect(validateAuth({ ...filled.auth, kind: "none" })).toEqual({});
  });

  it("auth: header rules match the engine (no 422 after a client-side pass)", () => {
    // Engine HEADER_RE ^[A-Za-z0-9][A-Za-z0-9-]{0,63}$ — RFC token chars like "_" or "." are refused there.
    expect(validateAuth({ ...filled.auth, header_name: "X_Api.Key" })["header_name"]).toBeDefined();
    expect(validateAuth({ ...filled.auth, header_name: "-X-Key" })["header_name"]).toBeDefined();
    expect(validateAuth({ ...filled.auth, header_name: `X${"a".repeat(64)}` })["header_name"]).toBeDefined();
    // Reserved transport / session headers (engine FORBIDDEN_HEADERS), any case.
    for (const h of ["Cookie", "host", "Proxy-Authorization", "Mcp-Session-Id", "Content-Type"]) {
      expect(validateAuth({ ...filled.auth, header_name: h })["header_name"]).toMatch(/reserved/);
    }
    expect(validateAuth({ ...filled.auth, header_name: "Authorization" })).toEqual({});
    // Whitespace-only, multi-line and oversized secrets are refused by the engine too.
    expect(validateAuth({ ...filled.auth, secret: "   " })["secret"]).toBeDefined();
    expect(validateAuth({ ...filled.auth, secret: "abc\r\nX-Evil: 1" })["secret"]).toMatch(/one line/);
    expect(validateAuth({ ...filled.auth, secret: "x".repeat(4097) })["secret"]).toBeDefined();
  });

  it("payload carries only the chosen kind's fields", () => {
    expect(authPayload({ ...filled.auth, kind: "none" })).toEqual({ kind: "none" });
    expect(
      authPayload({ ...filled.auth, kind: "oauth_client_credentials", token_url: " https://idp/token ", client_id: "c", client_secret: "s", scopes: "read  mcp,tools", resource: "" }),
    ).toEqual({ kind: "oauth_client_credentials", token_url: "https://idp/token", client_id: "c", client_secret: "s", scopes: ["read", "mcp", "tools"] });
    expect(createPayload(filled)).toEqual({
      slug: "erp-lite-test",
      name: "ERP Lite (test)",
      mcp_url: "http://merxa.localhost/mcp-lite",
      trust_level: "third_party",
      auth: { kind: "static_header", header_name: "X-Merxa-Api-Key", secret: "s3cret" },
    });
  });
});

describe("test step log", () => {
  it("idle: every step waiting; running: the first runs", () => {
    expect(testLogItems({ status: "idle" }).every((s) => s.state === "pending")).toBe(true);
    const r = testLogItems({ status: "running" });
    expect(r[0]!.state).toBe("running");
    expect(r.slice(1).every((s) => s.state === "pending")).toBe(true);
  });

  it("done: results per step, unreached steps skipped after a failure, details as text", () => {
    const items = testLogItems({ status: "done", result: failed });
    expect(items.map((s) => [s.id, s.state])).toEqual([
      ["url_policy", "ok"],
      ["reach", "failed"],
      ["discover", "skipped"],
      ["auth", "skipped"],
      ["tools_list", "skipped"],
      ["tier_detect", "skipped"],
      ["classify", "skipped"],
    ]);
    expect(items[1]!.detail).toBe('{"error":"connection refused"}');
    expect(items[1]!.ms).toBe(3000);
  });

  it("appends steps the server reports that the UI does not know", () => {
    const items = testLogItems({ status: "done", result: { ...passed, steps: [...passed.steps, { step: "egress_dns_pin", ok: true, ms: 2 }] } });
    expect(items.at(-1)).toMatchObject({ id: "egress_dns_pin", label: "Egress dns pin", state: "ok" });
  });
});
