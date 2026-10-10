/**
 * M7 back-office (`docs/contracts/110`): four-eyes Pending changes + inline "Requires a second
 * admin" notes, Users / Tenants, custom specialist builder + playground + workflow graph,
 * tier-A mapping editors + read test + dry-run + capabilities + crawl snapshot status, Signals
 * admin, Audit activity filters, Evals. Engine answer shapes from `routes_admin_m7.py`.
 */
import {
  adminChangeRequestSchema,
  adminUserSchema,
  agentsCustomSpecialistSchema,
  agentsPlaygroundResultSchema,
  connectorsCapabilitiesSchema,
  isProModel,
  mappingActionSchema,
  mappingDryRunResultSchema,
  mappingReadSchema,
  signalsAlertRuleSchema,
} from "@keelson/contracts";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AdminShell } from "@/components/admin/AdminShell";
import { AuditPage, activityQuery } from "@/components/admin/AuditPage";
import { ChangeRequestsPage, pointerLabel } from "@/components/admin/ChangeRequestsPage";
import { ConnectorDetail } from "@/components/admin/ConnectorDetail";
import { EvalResultsFolder } from "@/components/admin/evals/EvalResultsFolder";
import { KnowledgePage } from "@/components/admin/KnowledgePage";
import { MappingsPage } from "@/components/admin/MappingsPage";
import { SignalsPage } from "@/components/admin/SignalsPage";
import { SpecialistsPage } from "@/components/admin/SpecialistsPage";
import { TenantsPage } from "@/components/admin/TenantsPage";
import { UsersPage } from "@/components/admin/UsersPage";
import { WorkflowPage, workflowInputOf } from "@/components/admin/WorkflowPage";
import { activityDetail, activityPageSchema, dayBounds } from "@/lib/admin/audit";
import {
  approveState,
  canReject,
  decisionBody,
  decisionErrorText,
  decisionToast,
  effectiveStatus,
  expiresIn,
  guardedSchemaFor,
  kindLabel,
  pendingCountSchema,
  reasonLabel,
  sortRequests,
  type ChangeRequest,
} from "@/lib/admin/changeRequests";
import { caseScore, evalsResponseSchema, summaryTiles } from "@/lib/admin/evals";
import { checkMapping, convertText, expressionProblem, mappingEnvelopeSchema, mappingText, queuedSince, snapshotHistorySchema } from "@/lib/admin/mappings";
import { PLAYGROUND_IDLE, playgroundReduce, settlePlayground, startPlayground, usageLine } from "@/lib/admin/playground";
import {
  cadenceFromOption,
  cadenceOptions,
  hubErrorText,
  networkCodesSchema,
  parsePolygon,
  regionBody,
  regionIdFromName,
  regionSummary,
  signalSourceListSchema,
  watchRegionListSchema,
  type WatchRegion,
} from "@/lib/admin/signals";
import { aliasOptions, slugProblem, specialistIssues, withSpecialist, type CustomSpecialist } from "@/lib/admin/specialists";
import { catalogSchema } from "@/lib/admin/types";
import { grantableRoles, guardedRoleGrants, userChangeProblems, userListSchema } from "@/lib/admin/users";
import { expectNoA11yViolations } from "./a11y";
import { adminRoutes, makeAdminServer, type AdminServer } from "./adminMock";
import * as F from "./fixtures/m7";
import { fakeApi, jsonResponse, makeMe, withSession, type Route } from "./session";

const nav = vi.hoisted(() => ({ pathname: "/admin", push: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ push: nav.push, refresh: vi.fn() }) }));

const toastSpy = vi.hoisted(() => ({ calls: [] as { kind: string; title: unknown; opts: unknown }[] }));
vi.mock("@keelson/ui", async (orig) => {
  const mod = await orig<typeof import("@keelson/ui")>();
  const record = (kind: string) => (title: unknown, opts?: unknown) => {
    toastSpy.calls.push({ kind, title, opts });
    return "t";
  };
  const toast = Object.assign(record("info"), { success: record("success"), error: record("error"), dismiss: () => {} });
  return { ...mod, toast };
});

// CodeMirror and React Flow are loaded with next/dynamic in the app; tests use plain stand-ins.
vi.mock("@/components/admin/lazy", () => ({
  LazyCodeEditor: ({ value, onChange, label, describedBy }: { value: string; onChange: (v: string) => void; label: string; describedBy?: string }) => (
    <textarea aria-label={label} aria-describedby={describedBy} value={value} onChange={(e) => onChange(e.target.value)} />
  ),
  LazyWorkflowGraph: () => <div data-testid="workflow-graph" />,
}));

vi.setConfig({ testTimeout: 30_000 });

const SAM = () => makeMe({ sub: F.SUB_SAM, username: "sam", name: "Sam Okafor", roles: ["tenant-admin", "planner"], tenants: ["demo-retail"] });
const AVA = () => makeMe({ sub: F.SUB_AVA, username: "ava", name: "Ava Lindqvist", roles: ["platform-admin"], tenants: ["demo-retail", "northwind-test"] });

type M7Server = {
  changeRequests: Record<string, unknown>[];
  users: Record<string, unknown>[];
  liteConnector: Record<string, unknown>;
  liteTools: Record<string, unknown>[];
  mappings: { read: Record<string, unknown>; action: Record<string, unknown> };
  snapshots: Record<string, unknown>;
  sources: Record<string, unknown>;
  regions: Record<string, unknown>;
  rules: Record<string, unknown>;
  evals: Record<string, unknown>;
  /** `METHOD /path` → answer, checked first. */
  answers: Record<string, () => Response>;
  /** Who is signed in (set by `renderM7`): the decision route enforces four-eyes against it. */
  viewer: { sub: string; username: string; name: string; roles: string[] };
};

let base: AdminServer;
let m7: M7Server;

function makeM7(): M7Server {
  return {
    changeRequests: F.changeRequests(),
    users: F.users(),
    liteConnector: F.liteConnector(),
    liteTools: F.liteTools(),
    mappings: { read: F.mappingRow("read", 2), action: F.mappingRow("action", 1) },
    snapshots: F.snapshots(),
    sources: F.signalSources(),
    regions: F.watchRegions(),
    rules: F.alertRules(),
    evals: F.evals(),
    answers: {},
    viewer: { sub: F.SUB_SAM, username: "sam", name: "Sam Okafor", roles: ["tenant-admin", "planner"] },
  };
}

const bodyOf = (init: RequestInit): Record<string, unknown> => (init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {});

function sse(frames: unknown[]): Response {
  const text = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode(text));
      c.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

/** The M7 routes in front of the M2–M5 fake (`adminMock.ts`). */
function m7Routes(s: M7Server): Route {
  return (url, init) => {
    const method = init.method ?? "GET";
    const path = url.split("?")[0]!;
    const custom = s.answers[`${method} ${path}`];
    if (custom) return custom();
    if (!path.startsWith("/api/admin/")) return undefined;
    const p = path.slice("/api/admin".length);
    const q = new URL(url, "http://localhost").searchParams;
    let r: RegExpExecArray | null;
    if (method === "GET" && p === "/change-requests") {
      const status = q.get("status");
      const items = s.changeRequests.filter((c) => !status || c["status"] === status);
      return jsonResponse(200, { items, pending: s.changeRequests.filter((c) => c["status"] === "pending").length });
    }
    if ((r = /^\/change-requests\/([^/]+)\/(approve|reject)$/.exec(p)) && method === "POST") {
      const cr = s.changeRequests.find((c) => c["id"] === r![1]);
      if (!cr) return jsonResponse(404, { code: "not_found", message: "change request not found" });
      // The engine's rules (contracts-py `change_request_decision_error` + PLATFORM_KINDS / ASYNC_KINDS).
      const viewer = s.viewer;
      const reason = bodyOf(init)["reason"];
      if (cr["status"] !== "pending") return jsonResponse(409, { code: "not_pending", message: "this change request was already decided" });
      if (r[2] === "approve" && (cr["requested_by"] as { sub: string }).sub === viewer.sub) return jsonResponse(403, { code: "self_approval", message: "a different admin must approve your change (four-eyes)" });
      if (r[2] === "reject" && !(typeof reason === "string" && reason.trim())) return jsonResponse(422, { code: "reason_required", message: "a rejection needs a reason" });
      if (r[2] === "approve" && cr["kind"] === "embedding_profile" && !viewer.roles.includes("platform-admin")) return jsonResponse(403, { code: "platform_admin_required", message: "a platform admin approves it" });
      const updated = {
        ...cr,
        status: r[2] === "approve" ? (cr["kind"] === "embedding_profile" ? "approved" : "applied") : "rejected",
        approver: { sub: viewer.sub, username: viewer.username, name: viewer.name, role: viewer.roles.includes("platform-admin") ? "platform-admin" : "tenant-admin" },
        decided_at: "2026-10-08T10:00:00Z",
        decision_reason: bodyOf(init)["reason"] ?? null,
      };
      s.changeRequests = s.changeRequests.map((c) => (c["id"] === cr["id"] ? updated : c));
      return jsonResponse(200, updated);
    }
    if (method === "GET" && p === "/users") return jsonResponse(200, { items: s.users });
    if (method === "POST" && p === "/users") {
      const b = bodyOf(init);
      const roles = b["roles"] as string[];
      if (roles.includes("tenant-admin")) {
        return jsonResponse(202, F.changeRequest({ id: F.CR_USER, kind: "user_roles", target: `user:${String(b["username"])}`, base_version: null, requested_by: { sub: F.SUB_SAM, username: "sam", name: "Sam Okafor" }, reasons: [{ code: "admin_role_granted", field: `user:${String(b["username"])}`, reason: "grants tenant-admin" }] }));
      }
      const user = { id: "7a6b1c00-0000-4000-8000-0000000000ff", username: b["username"], email: b["email"], name: b["name"], enabled: true, roles, tenants: ["demo-retail"], created_at: "2026-10-08T10:00:00+00:00" };
      s.users = [...s.users, user];
      return jsonResponse(201, { user, setup: { link: null, link_expires_at: null, instructions: "We emailed lee@example.test a link to set a password.", required_action: "UPDATE_PASSWORD" } });
    }
    if ((r = /^\/users\/([^/]+)$/.exec(p))) {
      const u = s.users.find((x) => x["id"] === r![1]);
      if (!u) return jsonResponse(404, { code: "not_found", message: "user not found" });
      const next = method === "DELETE" ? { ...u, enabled: false } : { ...u, ...Object.fromEntries(Object.entries(bodyOf(init)).filter(([, v]) => v !== null)) };
      s.users = s.users.map((x) => (x["id"] === u["id"] ? next : x));
      return jsonResponse(200, next);
    }
    if ((r = /^\/tenants\/([^/]+)\/(suspend|reactivate)$/.exec(p)) && method === "POST") return jsonResponse(200, { slug: r[1], status: r[2] === "suspend" ? "suspended" : "active" });
    if (p === "/connectors/merxa-lite") {
      if (method === "GET") return jsonResponse(200, s.liteConnector);
      if (method === "PATCH") {
        const level = bodyOf(init)["trust_level"];
        if (level === "first_party") {
          return jsonResponse(202, F.changeRequest({ kind: "connector_trust", target: "connector:merxa-lite", base_version: null, reasons: [{ code: "first_party_trust_added", field: "connector:merxa-lite", reason: "adds a first-party trust level" }] }));
        }
        return jsonResponse(200, { slug: "merxa-lite", trust_level: level });
      }
    }
    if (method === "GET" && p === "/connectors/merxa-lite/tools") return jsonResponse(200, { items: s.liteTools });
    if (method === "GET" && p === "/connectors/merxa-lite/events") return jsonResponse(200, { items: [] });
    if ((r = /^\/connectors\/merxa-lite\/tools\/([^/]+)\/binding$/.exec(p)) && method === "PUT") {
      const tool = s.liteTools.find((t) => t["name"] === r![1])!;
      const binding = bodyOf(init)["binding"];
      if (binding === "auto" && tool["access"] !== "read") {
        return jsonResponse(202, F.changeRequest({ kind: "tool_binding", target: `connector:merxa-lite/tool:${String(tool["name"])}`, base_version: null, reasons: [{ code: "auto_binding_on_write", field: "x", reason: "lets agents call the write tool without approval" }] }));
      }
      return jsonResponse(200, { ...tool, binding });
    }
    if (method === "GET" && p === "/connectors/merxa-lite/capabilities") return jsonResponse(200, F.capabilities());
    if (method === "GET" && p === "/connectors/merxa-lite/snapshots") return jsonResponse(200, s.snapshots);
    if (method === "POST" && p === "/connectors/merxa-lite/sync") {
      // Engine: the request only marks the trigger state due; the worker starts the crawl later.
      s.snapshots = { ...s.snapshots, state: { stale: false, due_at: "2026-10-08T10:00:00+00:00", reason: "admin:sam" } };
      return jsonResponse(202, { status: "queued", connector: "merxa-lite", state: {}, current: null });
    }
    if ((r = /^\/connectors\/merxa-lite\/mappings\/(read|action)$/.exec(p))) {
      const kind = r[1] as "read" | "action";
      if (method === "GET") return jsonResponse(200, s.mappings[kind]);
      if (method === "PUT") {
        const b = bodyOf(init);
        const current = s.mappings[kind]["version"] as number | null;
        if (b["base_version"] !== current) return jsonResponse(409, { code: "version_conflict", message: "the mapping changed since you loaded it", latest_version: current });
        const version = (current ?? 0) + 1;
        s.mappings[kind] = { ...F.mappingRow(kind, version), mapping: b["mapping"] };
        return jsonResponse(201, { ...s.mappings[kind], unchanged: false });
      }
    }
    if (method === "POST" && p === "/connectors/merxa-lite/mappings/read/test") return jsonResponse(200, F.readPreview());
    if (method === "POST" && p === "/connectors/merxa-lite/mappings/action/dry-run") return jsonResponse(200, F.dryRun());
    if (method === "GET" && p === "/signals/sources") return jsonResponse(200, s.sources);
    if ((r = /^\/signals\/sources\/([^/]+)$/.exec(p)) && method === "PUT") {
      // Engine `put_source`: `{enabled?, cadence_s?}` (at least one; cadence 60…86,400 s or null = default).
      const b = bodyOf(init);
      const row = ((s.sources["items"] as Record<string, unknown>[]) ?? []).find((x) => x["id"] === r![1]) ?? {};
      if (!("enabled" in b) && !("cadence_s" in b)) return jsonResponse(422, { code: "invalid_request", message: "set enabled and/or cadence_s" });
      const cadence = "cadence_s" in b ? b["cadence_s"] : (row["cadence_s"] ?? null);
      if (cadence !== null && !(typeof cadence === "number" && cadence >= 60 && cadence <= 86_400)) return jsonResponse(422, { code: "invalid_request", message: "cadence_s out of range" });
      return jsonResponse(200, { id: r[1], enabled: "enabled" in b ? b["enabled"] : row["enabled"], cadence_s: cadence, hub_synced: false, hub_error: "signal_hub_unsupported" });
    }
    if (method === "GET" && p === "/signals/network") return jsonResponse(200, { nodes: { dc: ["DC-1", "DC-2"], port: ["VNSGN"], lane: ["x"], store: ["bad code with spaces"] } });
    if (p === "/signals/watch-regions") {
      if (method === "GET") return jsonResponse(200, s.regions);
      const regions = bodyOf(init)["regions"] as unknown[];
      s.regions = { items: regions };
      return jsonResponse(200, { items: regions, hub_synced: true, hub_error: null });
    }
    if (p === "/signals/alert-rules") {
      if (method === "GET") return jsonResponse(200, s.rules);
      const rules = bodyOf(init)["rules"] as unknown[];
      s.rules = { ...s.rules, custom: rules.length > 0, rules };
      return jsonResponse(200, { ...s.rules, hub_synced: true });
    }
    if (method === "GET" && p === "/evals") return jsonResponse(200, s.evals);
    if (method === "GET" && p === "/evals/files") return jsonResponse(200, s.evals);
    if (method === "GET" && p === "/agents/playground") return jsonResponse(200, { available: true, mode: "record_or_replay", reason: null, message: null });
    if (method === "POST" && p === "/agents/playground") return sse(F.playgroundFrames());
    if ((r = /^\/kb\/([^/]+)$/.exec(p)) && method === "DELETE") {
      return jsonResponse(202, F.changeRequest({ kind: "kb_delete", target: `kb:${r[1]}`, base_version: null, reasons: [{ code: "kb_deleted", field: "kb", reason: "deletes a knowledge base" }] }));
    }
    return undefined;
  };
}

const demoRoute: Route = (url) => (url.startsWith("/api/demo/packs") ? jsonResponse(200, { packs: [] }) : undefined);

function renderM7(ui: ReactNode, me = SAM()) {
  m7.viewer = { sub: me.user.sub, username: me.user.username, name: me.user.name ?? me.user.username, roles: [...me.user.roles] };
  const { api, calls } = fakeApi([m7Routes(m7), demoRoute, ...adminRoutes(base)]);
  const Wrapper = withSession(me, api);
  const utils = render(
    <Wrapper>
      <main>
        <AdminShell>{ui}</AdminShell>
      </main>
    </Wrapper>,
  );
  return { ...utils, calls };
}

const settle = () => act(async () => {});

beforeEach(() => {
  base = makeAdminServer();
  m7 = makeM7();
  nav.pathname = "/admin";
  nav.push.mockReset();
  toastSpy.calls = [];
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(F.NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

// ───────────────────────────── fixtures vs contracts ─────────────────────────────

describe("M7 fixtures match the generated contracts", () => {
  it("change requests, users, capabilities, mappings, dry-run, specialist, playground result, alert rules", () => {
    for (const c of F.changeRequests()) expect(adminChangeRequestSchema.safeParse(c).success, String(c["id"])).toBe(true);
    for (const u of F.users()) expect(adminUserSchema.safeParse(u).success, String(u["username"])).toBe(true);
    expect(connectorsCapabilitiesSchema.safeParse(F.capabilities()).success).toBe(true);
    expect(mappingReadSchema.safeParse(F.READ_MAPPING).success).toBe(true);
    expect(mappingActionSchema.safeParse(F.ACTION_MAPPING).success).toBe(true);
    expect(mappingDryRunResultSchema.safeParse(F.dryRun()).success).toBe(true);
    expect(agentsCustomSpecialistSchema.safeParse(F.SUPPLIER_RISK).success).toBe(true);
    expect(agentsPlaygroundResultSchema.safeParse(F.playgroundResult()).success).toBe(true);
    for (const r of (F.alertRules()["rules"] as unknown[])) expect(signalsAlertRuleSchema.safeParse(r).success).toBe(true);
  });
});

// ───────────────────────────── §1 four-eyes ─────────────────────────────

describe("change requests (lib)", () => {
  const list = () => F.changeRequests() as unknown as ChangeRequest[];

  it("self-approval is never offered; expired and decided requests can't be decided", () => {
    const [spec, own, user, old] = list();
    expect(approveState(spec!, F.SUB_SAM, F.NOW)).toEqual({ can: true });
    expect(approveState(own!, F.SUB_SAM, F.NOW)).toEqual({ can: false, why: "own" });
    expect(approveState(user!, F.SUB_SAM, F.NOW)).toEqual({ can: false, why: "expired" });
    expect(effectiveStatus(user!, F.NOW)).toBe("expired");
    expect(approveState(old!, F.SUB_SAM, F.NOW)).toEqual({ can: false, why: "not_pending" });
    expect(expiresIn(spec!, F.NOW)).toBe("Expires in 70 h");
    expect(expiresIn({ status: "pending", expires_at: "2026-10-08T10:20:00Z" }, F.NOW)).toBe("Expires in 20 min");
    expect(expiresIn(old!, F.NOW)).toBeNull();
    // Pending first (oldest first), then the rest newest first.
    expect(sortRequests(list(), F.NOW).map((c) => c.id)).toEqual([F.CR_SPEC, F.CR_OWN, F.CR_OLD, F.CR_USER]);
  });

  it("platform-wide kinds are approved by platform admins only; the requester may withdraw (reject), never approve", () => {
    const embed = F.changeRequest({ kind: "embedding_profile", target: "embedding_profile" }) as unknown as ChangeRequest;
    expect(approveState(embed, F.SUB_SAM, F.NOW, ["tenant-admin"])).toEqual({ can: false, why: "platform_only" });
    expect(approveState(embed, F.SUB_AVA, F.NOW, ["platform-admin"])).toEqual({ can: true });
    expect(canReject(embed, F.NOW)).toBe(true);
    const [, own, user, old] = list();
    expect(approveState(own!, F.SUB_SAM, F.NOW, ["platform-admin"])).toEqual({ can: false, why: "own" });
    expect(canReject(own!, F.NOW)).toBe(true);
    expect(canReject(user!, F.NOW)).toBe(false);
    expect(canReject(old!, F.NOW)).toBe(false);
    expect(decisionToast({ status: "approved", kind: "embedding_profile" }, "approve", false)).toBe("Change approved: it is being applied");
    expect(decisionToast({ status: "applied", kind: "agent_spec" }, "approve", false)).toBe("Change approved and applied");
    expect(decisionToast(null, "reject", true)).toBe("Request withdrawn");
    expect(decisionErrorText("not_pending", 409)).toMatch(/already decided/);
    expect(decisionErrorText("stale_base", 409)).toMatch(/settings changed/);
    expect(decisionErrorText("something_else", 409)).toMatch(/refresh/);
    expect(decisionErrorText("boom", 500)).toBeNull();
  });

  it("labels the new user_roles kind and admin_role_granted reason; unknown codes stay readable", () => {
    expect(kindLabel("user_roles")).toBe("User roles");
    expect(reasonLabel("admin_role_granted")).toBe("Grants an admin role");
    expect(kindLabel("future_kind")).toBe("future kind");
    expect(pointerLabel("/connectors/a~1b/tools")).toBe("connectors › a/b › tools");
  });

  it("decision notes are trimmed and capped; the pending count comes from the engine", () => {
    expect(decisionBody("  ok  ")).toEqual({ reason: "ok" });
    expect(decisionBody("   ")).toEqual({ reason: null });
    expect(decisionBody("x".repeat(501))).toBeNull();
    // A rejection needs a reason (engine: 422 reason_required).
    expect(decisionBody("   ", "reject")).toBeNull();
    expect(decisionBody(" no ", "reject")).toEqual({ reason: "no" });
    expect(pendingCountSchema.parse({ items: [], pending: 4 })).toBe(4);
    expect(pendingCountSchema.parse({ items: F.changeRequests() })).toBe(3);
  });

  it("a 202 answer must be a valid change request (never mistaken for 'applied')", () => {
    const pick = guardedSchemaFor(adminUserSchema);
    expect(pick(202).parse(F.changeRequest())).toMatchObject({ kind: "change_request", request: { id: F.CR_SPEC } });
    expect(pick(202).parse({ change_request: F.changeRequest() })).toMatchObject({ kind: "change_request" });
    expect(pick(202).safeParse({ ok: true }).success).toBe(false);
    expect(pick(200).parse(F.users()[0])).toMatchObject({ kind: "applied" });
  });
});

describe("Pending changes page", () => {
  it("lists requests waiting for a second admin; own requests have no buttons; expired ones drop out; axe", async () => {
    const { calls, container } = renderM7(<ChangeRequestsPage />);
    const list = await screen.findByRole("list", { name: "Change requests" });
    const cards = [...list.querySelectorAll<HTMLElement>(":scope > li")];
    expect(cards).toHaveLength(2);
    const spec = within(cards[0]!).getByRole("region", { name: /Agent settings/ });
    expect(within(spec).getByText("Raises an automatic-apply limit")).toBeInTheDocument();
    expect(within(spec).getByText(/Asked by Nora Berg/)).toHaveTextContent("Expires in 70 h");
    expect(within(spec).getByRole("rowheader", { name: "policy › auto_max_lines" })).toBeInTheDocument();
    expect(within(spec).getByRole("button", { name: "Approve" })).toBeInTheDocument();
    const own = within(cards[1]!).getByRole("region", { name: /Tool binding/ });
    expect(within(own).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(own).getByText(/You asked for this change/)).toBeInTheDocument();
    expect(calls.find((c) => c.url.startsWith("/api/admin/change-requests"))!.url).toBe("/api/admin/change-requests?status=pending&limit=200");
    await expectNoA11yViolations(container);
  });

  it("approve with a note → POST {reason}; the request reads 'Approved and applied'", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<ChangeRequestsPage />);
    const list = await screen.findByRole("list", { name: "Change requests" });
    await user.click(within(list).getAllByRole("button", { name: "Approve" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Approve this change?" });
    await user.type(within(dialog).getByRole("textbox"), "  Fine for peak  ");
    await user.click(within(dialog).getByRole("button", { name: "Approve and apply" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/approve"))).toBe(true));
    const post = calls.find((c) => c.url.endsWith("/approve"))!;
    expect(post).toMatchObject({ method: "POST", url: `/api/admin/change-requests/${F.CR_SPEC}/approve`, body: { reason: "Fine for peak" } });
    expect(post.headers["X-CSRF-Token"]).toBe("csrf-test");
    expect(toastSpy.calls.some((t) => t.kind === "success" && t.title === "Change approved and applied")).toBe(true);
  });

  it("a stale base version (409) explains that the settings moved; reject sends the reason", async () => {
    const user = userEvent.setup();
    m7.answers[`POST /api/admin/change-requests/${F.CR_SPEC}/approve`] = () => jsonResponse(409, { code: "stale_base", message: "the settings changed since the request was made" });
    const { calls } = renderM7(<ChangeRequestsPage />);
    const list = await screen.findByRole("list", { name: "Change requests" });
    await user.click(within(list).getAllByRole("button", { name: "Approve" })[0]!);
    let dialog = await screen.findByRole("dialog", { name: "Approve this change?" });
    await user.click(within(dialog).getByRole("button", { name: "Approve and apply" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/settings changed after this request was made/);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await user.click(within(list).getAllByRole("button", { name: "Reject" })[0]!);
    dialog = await screen.findByRole("dialog", { name: "Reject this change?" });
    await user.type(within(dialog).getByRole("textbox"), "Too risky before peak");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/reject"))).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/reject"))!.body).toEqual({ reason: "Too risky before peak" });
  });

  it("a rejection needs a reason: nothing is sent until one is given", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<ChangeRequestsPage />);
    const list = await screen.findByRole("list", { name: "Change requests" });
    await user.click(within(list).getAllByRole("button", { name: "Reject" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Reject this change?" });
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    expect(await within(dialog).findByText("Say why: the requester sees it.")).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/reject"))).toBe(false);
    await user.type(within(dialog).getByRole("textbox"), "Not before peak");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/reject"))).toBe(true));
    expect(toastSpy.calls.some((t) => t.kind === "success" && t.title === "Change rejected")).toBe(true);
  });

  it("your own request offers only 'Withdraw' (a reject with a reason), never 'Approve'", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<ChangeRequestsPage />);
    const own = (await screen.findAllByRole("region", { name: /Tool binding/ }))[0]!;
    expect(within(own).queryByRole("button", { name: "Approve" })).toBeNull();
    await user.click(within(own).getByRole("button", { name: /Withdraw your request/ }));
    const dialog = await screen.findByRole("dialog", { name: "Withdraw your request?" });
    await user.type(within(dialog).getByRole("textbox"), "Not needed any more");
    await user.click(within(dialog).getByRole("button", { name: "Withdraw request" }));
    await waitFor(() => expect(calls.some((c) => c.url === `/api/admin/change-requests/${F.CR_OWN}/reject`)).toBe(true));
    expect(calls.some((c) => c.url === `/api/admin/change-requests/${F.CR_OWN}/approve`)).toBe(false);
    expect(toastSpy.calls.some((t) => t.kind === "success" && t.title === "Request withdrawn")).toBe(true);
  });

  it("an embedding-profile change: tenant admins may reject but not approve; a platform admin approves (being applied)", async () => {
    m7.changeRequests = [F.changeRequest({ kind: "embedding_profile", target: "embedding_profile", reasons: [{ code: "embedding_profile_changed", field: "embedding_profile", reason: "changes the active embedding profile" }] })];
    const { unmount } = renderM7(<ChangeRequestsPage />);
    let card = (await screen.findAllByRole("region", { name: /Embedding profile/ }))[0]!;
    expect(within(card).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(card).getByText(/only a platform admin can approve it/)).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Reject" })).toBeInTheDocument();
    unmount();
    const user = userEvent.setup();
    const { calls } = renderM7(<ChangeRequestsPage />, AVA());
    card = (await screen.findAllByRole("region", { name: /Embedding profile/ }))[0]!;
    await user.click(within(card).getByRole("button", { name: "Approve" }));
    const dialog = await screen.findByRole("dialog", { name: "Approve this change?" });
    await user.click(within(dialog).getByRole("button", { name: "Approve and apply" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/approve"))).toBe(true));
    await waitFor(() => expect(toastSpy.calls.some((t) => t.kind === "success" && t.title === "Change approved: it is being applied")).toBe(true));
  });

  it("a request another admin decided meanwhile (409 not_pending) says so instead of 'settings changed'", async () => {
    const user = userEvent.setup();
    m7.answers[`POST /api/admin/change-requests/${F.CR_SPEC}/approve`] = () => jsonResponse(409, { code: "not_pending", message: "this change request was already decided", status_now: "applied" });
    renderM7(<ChangeRequestsPage />);
    const list = await screen.findByRole("list", { name: "Change requests" });
    await user.click(within(list).getAllByRole("button", { name: "Approve" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Approve this change?" });
    await user.click(within(dialog).getByRole("button", { name: "Approve and apply" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Another admin already decided this request.");
    expect(alert).not.toHaveTextContent(/settings changed/);
  });

  it("'All requests' shows decided ones with the approver and reason (no status filter sent)", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<ChangeRequestsPage />, AVA());
    await screen.findByRole("list", { name: "Change requests" });
    await user.selectOptions(screen.getByLabelText("Show"), "all");
    expect(await screen.findByText(/Rejected by Ava Lindqvist \(platform admin\)/)).toHaveTextContent("Still used by the playbook analyst");
    expect(calls.at(-1)!.url).toBe("/api/admin/change-requests?limit=200");
    const expired = document.querySelector(`[data-change-request="${F.CR_USER}"]`)!;
    expect(expired.getAttribute("data-status")).toBe("expired");
    expect(within(expired as HTMLElement).getByText("User roles")).toBeInTheDocument();
    expect(within(expired as HTMLElement).getByText("Grants an admin role")).toBeInTheDocument();
    // A platform admin co-signs any request — including Sam's own.
    expect(within(document.querySelector(`[data-change-request="${F.CR_OWN}"]`) as HTMLElement).getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("the admin nav shows the engine's pending count; Evals is for platform admins only", async () => {
    m7.answers["GET /api/admin/change-requests"] = () => jsonResponse(200, { items: [], pending: 5 });
    const { unmount } = renderM7(<p>x</p>);
    const navEl = screen.getByRole("navigation", { name: "Admin" });
    expect(await within(navEl).findByRole("link", { name: /Pending changes, 5 waiting/ })).toBeInTheDocument();
    expect(within(navEl).queryByRole("link", { name: "Evals" })).toBeNull();
    unmount();
    renderM7(<p>x</p>, AVA());
    expect(screen.getByRole("link", { name: "Evals" })).toBeInTheDocument();
  });
});

describe("guarded saves elsewhere become change requests (202)", () => {
  it("tool binding 'Automatic' on a write tool: inline note, then 'Sent for approval' → Pending changes", async () => {
    const user = userEvent.setup();
    nav.pathname = "/admin/connectors/merxa-lite";
    const { calls } = renderM7(<ConnectorDetail slug="merxa-lite" />);
    const table = await screen.findByRole("table", { name: "Tools" });
    const row = within(table).getByRole("rowheader", { name: /update_item/ }).closest("tr")!;
    expect(within(row).getByText("Automatic needs a second admin")).toBeInTheDocument();
    await user.selectOptions(within(row).getByLabelText("When agents call update_item"), "auto");
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/admin/connectors/merxa-lite/tools/update_item/binding")).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/binding"))!.body).toEqual({ binding: "auto" });
    const sent = toastSpy.calls.find((t) => t.title === "Sent for approval")!;
    expect(sent).toBeDefined();
    (sent.opts as { action: { onClick: () => void } }).action.onClick();
    expect(nav.push).toHaveBeenCalledWith(`/admin/changes?request=${F.CR_SPEC}`);
    // A read tool's binding applies directly.
    await user.selectOptions(within(table).getByLabelText("When agents call list_notes"), "deny");
    await waitFor(() => expect(toastSpy.calls.some((t) => t.kind === "success" && String(t.title).startsWith("list_notes"))).toBe(true));
  });

  it("trust level first party for a tenant admin: note + change request; platform admins see no note", async () => {
    const user = userEvent.setup();
    const { calls, unmount } = renderM7(<ConnectorDetail slug="merxa-lite" />);
    const trust = await screen.findByLabelText("Trust level of Northwind ERP (lite)");
    expect(screen.getByText("Making it first party needs a second admin")).toBeInTheDocument();
    await user.selectOptions(trust, "first_party");
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.url === "/api/admin/connectors/merxa-lite")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ trust_level: "first_party" });
    await waitFor(() => expect(toastSpy.calls.some((t) => t.title === "Sent for approval")).toBe(true));
    unmount();
    renderM7(<ConnectorDetail slug="merxa-lite" />, AVA());
    await screen.findByLabelText("Trust level of Northwind ERP (lite)");
    expect(screen.queryByText("Making it first party needs a second admin")).toBeNull();
  });

  it("deleting a knowledge base is requested, not applied, for a tenant admin", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<KnowledgePage />);
    await user.click(await screen.findByRole("button", { name: "Delete Playbooks & policies" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Delete Playbooks & policies?" });
    expect(confirm).toHaveTextContent("Deleting needs a second admin");
    await user.click(within(confirm).getByRole("button", { name: "Request deletion" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/admin/kb/kb-1")).toBe(true));
    await waitFor(() => expect(toastSpy.calls.some((t) => t.title === "Sent for approval")).toBe(true));
  });
});

// ───────────────────────────── §2 users & tenants ─────────────────────────────

describe("users (lib)", () => {
  const all = userListSchema.parse({ items: F.users() });
  const byName = (n: string) => all.find((u) => u.username === n)!;
  const sam = { roles: ["tenant-admin", "planner"], tenants: ["demo-retail"] };

  it("tenant admins can't grant platform admin; platform admins can grant everything", () => {
    expect(grantableRoles(sam.roles)).not.toContain("platform-admin");
    expect(grantableRoles(["platform-admin"])).toContain("platform-admin");
    expect(grantableRoles(["planner"])).toEqual([]);
  });

  it("a user who also belongs to another workspace is out of a tenant admin's reach (cross-tenant guard)", () => {
    expect(userChangeProblems(sam, byName("pat"), { enabled: false }, "demo-retail")[0]).toMatch(/also belongs to northwind-test/);
    expect(userChangeProblems({ roles: ["platform-admin"], tenants: [] }, byName("pat"), { enabled: false }, "demo-retail")).toEqual([]);
    expect(userChangeProblems(sam, byName("nora"), { newRoles: ["approver", "platform-admin"] }, "demo-retail")[0]).toMatch(/can't grant or remove Platform admin/);
    expect(userChangeProblems(sam, null, { newRoles: ["planner"] }, "demo-retail")).toEqual([]);
  });

  it("granting an admin role (or re-enabling a disabled admin) is guarded for tenant admins only", () => {
    expect(guardedRoleGrants(sam.roles, null, { newRoles: ["tenant-admin"] })).toEqual(["tenant-admin"]);
    expect(guardedRoleGrants(sam.roles, byName("nora"), { newRoles: ["approver"] })).toEqual([]);
    expect(guardedRoleGrants(sam.roles, { roles: ["tenant-admin"], enabled: false }, { enabled: true })).toEqual(["tenant-admin"]);
    expect(guardedRoleGrants(["platform-admin"], null, { newRoles: ["tenant-admin"] })).toEqual([]);
  });
});

describe("Users page", () => {
  it("lists users; shared and own accounts can't be changed here; axe", async () => {
    const { container } = renderM7(<UsersPage />);
    const table = await screen.findByRole("table", { name: "Users" });
    const row = (u: string) => table.querySelector(`tr[data-user="${u}"]`) as HTMLElement;
    expect(within(row("pat")).getByRole("button", { name: "Disable pat" })).toHaveAttribute("aria-disabled", "true");
    expect(within(row("pat")).getByText(/also belongs to northwind-test/)).toBeInTheDocument();
    expect(within(row("sam")).getByText("You can't change your own account here.")).toBeInTheDocument();
    expect(within(row("dee")).getByRole("button", { name: "Enable dee" })).not.toHaveAttribute("aria-disabled", "true");
    expect(within(row("ava")).getByText("Platform admin")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("invite a planner: the one-time instructions are shown, never a password", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<UsersPage />);
    await user.click(await screen.findByRole("button", { name: "Invite user" }));
    const dialog = await screen.findByRole("dialog", { name: "Invite a user" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Lee Park");
    await user.type(within(dialog).getByLabelText(/^Email/), "lee@example.test");
    await user.type(within(dialog).getByLabelText(/^Username/), "Lee");
    expect(within(dialog).getByRole("checkbox", { name: /Platform admin/ })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Invite" }));
    const done = await screen.findByRole("dialog", { name: "Lee Park was invited" });
    expect(done).toHaveTextContent("We emailed lee@example.test a link to set a password.");
    expect(done).not.toHaveTextContent(/password:/i);
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ username: "lee", email: "lee@example.test", name: "Lee Park", roles: ["planner"] });
  });

  it("inviting a tenant admin shows 'Requires a second admin' and sends a change request", async () => {
    const user = userEvent.setup();
    renderM7(<UsersPage />);
    await user.click(await screen.findByRole("button", { name: "Invite user" }));
    const dialog = await screen.findByRole("dialog", { name: "Invite a user" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Lee Park");
    await user.type(within(dialog).getByLabelText(/^Email/), "lee@example.test");
    await user.type(within(dialog).getByLabelText(/^Username/), "lee");
    await user.click(within(dialog).getByRole("checkbox", { name: /Tenant admin/ }));
    const note = within(dialog).getByRole("note");
    expect(note).toHaveTextContent("Requires a second admin");
    expect(note).toHaveTextContent("Grants the Tenant admin role");
    await user.click(within(dialog).getByRole("button", { name: "Request invite" }));
    await waitFor(() => expect(toastSpy.calls.some((t) => t.title === "Sent for approval")).toBe(true));
    expect(screen.queryByRole("dialog", { name: /was invited/ })).toBeNull();
  });

  it("disable → DELETE (the account is kept, shown as disabled)", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<UsersPage />);
    await user.click(await screen.findByRole("button", { name: "Disable nora" }));
    const confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getAllByRole("button").at(-1)!);
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === `/api/admin/users/${F.USER_ID.nora}`)).toBe(true));
  });
});

describe("Tenants page", () => {
  it("suspend and reactivate call the engine's routes", async () => {
    const user = userEvent.setup();
    base.tenants = [...base.tenants, { slug: "old-co", name: "Old Co", pack: "blank", status: "suspended" } as never];
    const { calls } = renderM7(<TenantsPage />, AVA());
    await user.click(await screen.findByRole("button", { name: "Suspend Isolation test tenant" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Suspend tenant" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/tenants/northwind-test/suspend" && c.method === "POST")).toBe(true));
    await user.click(screen.getByRole("button", { name: "Resume Old Co" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Resume tenant" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/tenants/old-co/reactivate")).toBe(true));
  });
});

// ───────────────────────────── §3 agents ─────────────────────────────

function catalogWithPro() {
  const cat = structuredClone(base.catalog) as Record<string, unknown> & { aliases: unknown[] };
  cat.aliases = [...cat.aliases, { alias: "gemini-2-5-pro", mode: "chat", capabilities: { structured_output: true, reasoning_stream: true, tool_loop_signatures: true } }];
  return cat;
}

describe("custom specialists (lib)", () => {
  const catalog = () => catalogSchema.parse(catalogWithPro());
  const draft = (over: Partial<CustomSpecialist> = {}): CustomSpecialist => ({ ...(structuredClone(F.SUPPLIER_RISK) as unknown as CustomSpecialist), ...over });
  const ctx = (over = {}) => ({ others: [] as CustomSpecialist[], builtInSlugs: ["inventory_demand", "store_ops"], catalog: catalog(), kbIds: [] as string[], isNew: true, ...over });

  it("Pro models are listed but can't be chosen (isProModel parity incl. 'Gemini 2.5 Pro')", () => {
    const pro = aliasOptions(catalog()).find((a) => a.value === "gemini-2-5-pro")!;
    expect(pro).toMatchObject({ disabled: true, reason: "Pro models aren't allowed" });
    expect(isProModel("Gemini 2.5 Pro")).toBe(true);
    expect(isProModel("prod-fast")).toBe(false);
    expect(specialistIssues(draft({ model_alias: "gemini-2-5-pro" }), ctx()).model_alias).toMatch(/Pro models aren't allowed/);
  });

  it("only approved read tools; built-in slugs and reserved names are refused", () => {
    expect(specialistIssues(draft(), ctx())).toEqual({});
    expect(specialistIssues(draft({ tools: [{ connector: "merxa", tool: "apply_change_set" }] }), ctx()).tools).toMatch(/Not allowed: merxa\/apply_change_set/);
    expect(specialistIssues(draft({ tools: [{ connector: "erp-lite", tool: "list_items" }] }), ctx()).tools).toMatch(/Not allowed/); // connector not active
    expect(specialistIssues(draft({ slug: "store_ops" }), ctx()).slug).toBe("Another specialist already uses this id.");
    expect(specialistIssues(draft({ slug: "inventory_demand" }), ctx()).slug).toMatch(/built-in agent/);
    expect(slugProblem("planner")).toMatch(/built-in agent/);
    expect(slugProblem("risk_investigator")).toMatch(/_investigator/);
    expect(slugProblem("Bad")).toMatch(/lower-case/);
    expect(specialistIssues(draft({ tools: [], kb_ids: [] }), ctx()).tools).toMatch(/at least one tool or knowledge base/);
  });

  it("the merge patch replaces the whole list, keeping the order", () => {
    const a = draft({ slug: "aaa_risk", display_name: "A" });
    const b = draft({ slug: "bbb_risk", display_name: "B" });
    expect(withSpecialist([a, b], { ...a, display_name: " A2 " }, "aaa_risk").map((s) => [s.slug, s.display_name])).toEqual([
      ["aaa_risk", "A2"],
      ["bbb_risk", "B"],
    ]);
    expect(withSpecialist([a, b], null, "aaa_risk").map((s) => s.slug)).toEqual(["bbb_risk"]);
  });
});

describe("playground stream (lib)", () => {
  it("folds the Translator's frames into activity + result; a stream that ends without one is an error", () => {
    let s = startPlayground();
    for (const f of F.playgroundFrames()) s = playgroundReduce(s, f);
    expect(s.status).toBe("done");
    expect(s.agent).toBe("Supplier risk analyst");
    expect(s.activities).toEqual([{ id: "fc-1", label: "Read 12 purchase orders from Merxa", state: "done" }]);
    expect(usageLine(s.result!)).toBe("3 tool calls · 4 model calls · 12,400 tokens · 8.2 s");
    expect(settlePlayground(startPlayground())).toMatchObject({ status: "error", error: "The playground stopped without a result." });
    const budget = playgroundReduce(startPlayground(), F.playgroundResult({ status: "budget_exceeded", findings: null, error: { code: "budget_exceeded", message: "llm calls" } }));
    expect(budget.status).toBe("error");
    expect(playgroundReduce(PLAYGROUND_IDLE, { type: "NOT_AN_EVENT" })).toBe(PLAYGROUND_IDLE);
  });
});

describe("Custom specialists page", () => {
  beforeEach(() => {
    (base.spec.spec as Record<string, unknown>)["custom_specialists"] = [structuredClone(F.SUPPLIER_RISK)];
    base.catalog = catalogWithPro() as typeof base.catalog;
  });

  it("builder: tool picker by connector (write tools disabled, untrusted quarantined), Pro alias disabled, save → spec PUT", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderM7(<SpecialistsPage />);
    expect(await screen.findByRole("region", { name: /Supplier risk analyst/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New specialist" }));
    const drawer = await screen.findByRole("dialog", { name: "New specialist" });
    await user.type(within(drawer).getByLabelText(/^Display name/), "Lead time watcher");
    expect(within(drawer).getByLabelText(/^Id/)).toHaveValue("lead_time_watcher");
    await user.type(within(drawer).getByLabelText(/^What it checks/), "Watches supplier lead times.");
    await user.type(within(drawer).getByRole("textbox", { name: /^Role prompt/ }), "Compare lead times with the plan.");
    const model = within(drawer).getByLabelText(/^Model/);
    expect(within(model).getByRole("option", { name: /gemini-2-5-pro: Pro models aren't allowed/ })).toBeDisabled();
    await user.selectOptions(model, "fast-default");
    const merxa = within(drawer).getByRole("group", { name: "Tools of Merxa" });
    expect(within(merxa).getByRole("checkbox", { name: /apply_change_set/ })).toBeDisabled();
    expect(within(merxa).getByRole("checkbox", { name: /list_supplier_messages/ })).toBeEnabled();
    await user.click(within(merxa).getByRole("checkbox", { name: /list_purchase_orders/ }));
    await expectNoA11yViolations(container);
    await user.click(within(drawer).getByRole("button", { name: "Create specialist" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/admin/agents/spec")).toBe(true));
    const put = calls.find((c) => c.method === "PUT")!.body as { base_version: string; spec_patch: { custom_specialists: { slug: string; tools: unknown[]; model_alias: string }[] } };
    expect(put.base_version).toBe("v3");
    expect(put.spec_patch.custom_specialists.map((s) => s.slug)).toEqual(["supplier_risk", "lead_time_watcher"]);
    expect(put.spec_patch.custom_specialists[1]).toMatchObject({ model_alias: "fast-default", tools: [{ connector: "merxa", tool: "list_purchase_orders" }] });
  });

  it("while the knowledge bases load, the editor waits (a saved specialist's KBs never read as deleted)", async () => {
    const kbId = "0b9c1f9e-1c1a-4f8e-9a51-2b5b7a0c00bb";
    const kbs = [{ ...base.kbs[0]!, id: kbId, name: "Supplier playbook" }];
    (base.spec.spec as Record<string, unknown>)["custom_specialists"] = [{ ...structuredClone(F.SUPPLIER_RISK), kb_ids: [kbId] }];
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    m7.answers["GET /api/admin/kb"] = (() => gate.then(() => jsonResponse(200, { items: kbs, profile: base.kbProfile }))) as unknown as () => Response;
    const user = userEvent.setup();
    renderM7(<SpecialistsPage />);
    expect(await screen.findByRole("status", { name: "Loading custom specialists" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Supplier risk analyst/ })).toBeNull();
    await act(async () => release());
    expect(await screen.findByRole("region", { name: /Supplier risk analyst/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Edit Supplier risk analyst/ }));
    const drawer = await screen.findByRole("dialog", { name: /Edit Supplier risk analyst/ });
    expect(within(drawer).queryByText(/no longer exists/)).toBeNull();
    expect(within(drawer).getByRole("checkbox", { name: /Supplier playbook/ })).toBeChecked();
  });

  it("knowledge bases that can't be loaded don't make a saved specialist's KBs read as deleted", async () => {
    (base.spec.spec as Record<string, unknown>)["custom_specialists"] = [{ ...structuredClone(F.SUPPLIER_RISK), kb_ids: ["0b9c1f9e-1c1a-4f8e-9a51-2b5b7a0c00aa"] }];
    m7.answers["GET /api/admin/kb"] = () => jsonResponse(503, { code: "unavailable", message: "down" });
    const user = userEvent.setup();
    renderM7(<SpecialistsPage />);
    expect(await screen.findByRole("region", { name: /Supplier risk analyst/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Edit Supplier risk analyst/ }));
    const drawer = await screen.findByRole("dialog", { name: /Edit Supplier risk analyst/ });
    expect(within(drawer).getByText("Knowledge bases couldn't be loaded.")).toBeInTheDocument();
    expect(within(drawer).queryByText(/no longer exists/)).toBeNull();
  });

  it("playground: streams the activity and shows findings for the saved specialist", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<SpecialistsPage />);
    const ask = await screen.findByLabelText(/^Ask Supplier risk analyst/);
    await user.type(ask, "Which trim POs are late?");
    await user.click(screen.getByRole("button", { name: "Run in playground" }));
    const activity = await screen.findByRole("region", { name: "Playground activity" });
    expect(await within(activity).findByText("Two trim POs are 9 days late.")).toBeInTheDocument();
    expect(within(activity).getByText("Read 12 purchase orders from Merxa")).toBeInTheDocument();
    expect(within(activity).getByText(/only its structured extraction reached the specialist/)).toBeInTheDocument();
    expect(within(activity).getByText("3 tool calls · 4 model calls · 12,400 tokens · 8.2 s")).toBeInTheDocument();
    const post = calls.find((c) => c.url === "/api/admin/agents/playground" && c.method === "POST")!;
    expect(post.body).toMatchObject({ question: "Which trim POs are late?" });
    expect(post.headers["Accept"]).toContain("text/event-stream");
  });

  it("playground: disabled while the engine only replays recorded model answers", async () => {
    m7.answers["GET /api/admin/agents/playground"] = () => jsonResponse(200, { available: false, mode: "replay", reason: "replay_only", message: "replay only" });
    const user = userEvent.setup();
    const { calls } = renderM7(<SpecialistsPage />);
    const ask = await screen.findByLabelText(/^Ask Supplier risk analyst/);
    await user.type(ask, "Which trim POs are late?");
    expect(await screen.findByText(/only replays recorded model answers/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run in playground" })).toHaveAttribute("aria-disabled", "true");
    expect(calls.some((c) => c.url === "/api/admin/agents/playground" && c.method === "POST")).toBe(false);
  });

  it("workflow graph input: built-in + custom specialists; the outline lists them by stage", async () => {
    const input = workflowInputOf(base.spec.spec as never);
    expect(input.specialists.map((s) => [s.slug, s.custom, s.enabled])).toEqual([
      ["inventory_demand", false, true],
      ["supplier_risk", true, true],
    ]);
    expect(input.specialists[1]).toMatchObject({ connectors: ["merxa"], tools: 2 });
    const { container } = renderM7(<WorkflowPage />);
    const outline = await screen.findByRole("list", { name: "Workflow steps by stage" });
    const custom = outline.querySelector('[data-workflow-node="spec:supplier_risk"]') as HTMLElement;
    expect(within(custom).getByText("Supplier risk analyst")).toBeInTheDocument();
    expect(within(custom).getByText("Custom specialist")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-graph")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });
});

// ───────────────────────────── §4 tier-A connectors ─────────────────────────────

describe("mappings (lib)", () => {
  const tools = F.liteTools() as never;
  const yaml = (o: unknown) => mappingText({ mapping: o as Record<string, unknown>, version: 1, problems: [] } as never, "yaml");

  it("a valid read mapping passes; the engine's envelope with no mapping reads as none", () => {
    expect(checkMapping("read", yaml(F.READ_MAPPING), tools).value).not.toBeNull();
    const none = mappingEnvelopeSchema.parse(F.mappingRow("read", null));
    expect(none.mapping).toBeNull();
    expect(none.version).toBeNull();
    expect(mappingText(none, "yaml")).toBe("");
    expect(convertText(yaml(F.READ_MAPPING), "json")).toContain('"entity": "location"');
  });

  it("crawl sources must be approved trusted read tools; errors point at the line", () => {
    const write = structuredClone(F.READ_MAPPING) as { entities: { source: { tool: string } }[] };
    write.entities[0]!.source.tool = "update_item";
    const out = checkMapping("read", yaml(write), tools);
    expect(out.value).toBeNull();
    const d = out.diagnostics.find((x) => x.message.includes("update_item"))!;
    expect(d.message).toMatch(/is a write tool/);
    expect(d.line).toBe(5);
    write.entities[0]!.source.tool = "list_notes";
    expect(checkMapping("read", yaml(write), tools).diagnostics.map((x) => x.message).join()).toMatch(/untrusted text/);
  });

  it("expressions are looked at, never run: unknown functions, dunder names and brackets", () => {
    expect(expressionProblem("int($.qty)")).toBeNull();
    expect(expressionProblem("eval($.x)")).toMatch(/Unknown function "eval"/);
    expect(expressionProblem("$.__class__")).toMatch(/double underscore/);
    expect(expressionProblem("int($.qty")).toMatch(/not closed/);
    expect(expressionProblem("'eval(' + $.a")).toBeNull();
  });

  it("wrong kind, bad YAML and action warnings", () => {
    expect(checkMapping("action", yaml(F.READ_MAPPING)).diagnostics[0]!.message).toMatch(/"kind" must be "action"/);
    expect(checkMapping("read", "kind: read\n  bad: [").diagnostics[0]!.severity).toBe("error");
    const action = checkMapping("action", yaml(F.ACTION_MAPPING), tools);
    expect(action.value).not.toBeNull();
    const noneOp = structuredClone(F.ACTION_MAPPING) as { ops: Record<string, unknown>[] };
    noneOp.ops[0]!["idempotency"] = "none";
    expect(checkMapping("action", yaml(noneOp), tools).diagnostics.some((x) => x.severity === "warning" && /never retried automatically/.test(x.message))).toBe(true);
  });

  it("snapshot history: crawled consistency, as-of range and truncated entities", () => {
    const h = snapshotHistorySchema.parse(F.snapshots());
    expect(h.current).toMatchObject({ consistency: "crawled", as_of_range: { from: "2026-10-08T09:12:00+00:00" } });
    expect(h.current!.crawl!.truncated).toEqual(["inventory_position"]);
    expect(h.items).toHaveLength(2);
    expect(snapshotHistorySchema.parse({ current: null, items: [{ bad: 1 }] })).toEqual({ current: null, state: null, items: [] });
    expect(queuedSince(h)).toBeNull();
    expect(queuedSince(snapshotHistorySchema.parse(F.snapshots({ state: { due_at: "2026-10-08T10:00:00+00:00" } })))).toBe("2026-10-08T10:00:00+00:00");
    // While an attempt is loading it is "running", not queued.
    expect(queuedSince(snapshotHistorySchema.parse(F.snapshots({ state: { due_at: "2026-10-08T10:00:00+00:00" }, items: [{ id: "s", status: "loading", row_counts: {} }] })))).toBeNull();
  });
});

describe("Mappings page", () => {
  beforeEach(() => {
    base.connectors = [...base.connectors, F.liteConnector()];
    nav.pathname = "/admin/connectors/merxa-lite/mappings";
  });

  it("capabilities + read editor: live validation, save sends {base_version, mapping} only; axe", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderM7(<MappingsPage slug="merxa-lite" />);
    const caps = await screen.findByRole("region", { name: "Capabilities" });
    expect(await within(caps).findByText("Crawl: pages through mapped read tools (tier A)")).toBeInTheDocument();
    expect(within(caps).getByText("One call per change, with a ledger and read-back")).toBeInTheDocument();
    expect(within(caps).getByText("Crawled")).toBeInTheDocument();
    expect(within(caps).getByRole("note")).toHaveTextContent("advisory");
    const editor = await screen.findByRole("textbox", { name: "Read mapping (YAML)" });
    expect((editor as HTMLTextAreaElement).value).toContain("tool: list_items");
    expect(screen.getByText(/Saved/)).toHaveTextContent("by sam");
    await user.clear(editor);
    await user.click(editor);
    await user.paste("kind: read\nentities: []\n");
    expect(await screen.findByText(/problem(s)? to fix before saving/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save mapping" })).toHaveAttribute("aria-disabled", "true");
    await user.clear(editor);
    await user.click(editor);
    await user.paste(mappingText({ mapping: F.READ_MAPPING, version: 1, problems: [] } as never, "yaml").replace("max_pages: 20", "max_pages: 30"));
    expect(await screen.findByText("Valid against the mapping contract.")).toBeInTheDocument();
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: "Save mapping" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toBe("/api/admin/connectors/merxa-lite/mappings/read");
    expect(Object.keys(put.body as object).sort()).toEqual(["base_version", "mapping"]);
    expect((put.body as { base_version: number }).base_version).toBe(2);
    expect(JSON.stringify((put.body as { mapping: unknown }).mapping)).toContain('"max_pages":30');
  });

  it("test with one page: sends the draft, shows mapped sample rows and the first error per entity", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<MappingsPage slug="merxa-lite" />);
    const editor = await screen.findByRole("textbox", { name: "Read mapping (YAML)" });
    await user.click(screen.getByRole("button", { name: "Test with one page" }));
    const preview = await waitFor(() => document.querySelector("[data-read-preview]") as HTMLElement);
    expect(preview).toHaveTextContent("Test of the saved mapping");
    expect(calls.find((c) => c.url.endsWith("/read/test"))!.body).toEqual({ max_pages: 1 });
    expect(within(preview).getByRole("table", { name: "Sample rows of location" })).toHaveTextContent("Rotterdam DC");
    expect(preview).toHaveTextContent("expression_error: int() of 'n/a'");
    expect(preview).toHaveTextContent("More pages exist for location");
    await user.clear(editor);
    await user.click(editor);
    await user.paste(mappingText({ mapping: F.READ_MAPPING, version: 1, problems: [] } as never, "yaml").replace("max_pages: 20", "max_pages: 5"));
    await user.click(screen.getByRole("button", { name: "Test with one page" }));
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/read/test"))).toHaveLength(2));
    expect((calls.filter((c) => c.url.endsWith("/read/test")).at(-1)!.body as { mapping: unknown }).mapping).toMatchObject({ kind: "read" });
  });

  it("a newer saved version (409) offers to load it; saved mappings failing today's tool checks are flagged", async () => {
    const user = userEvent.setup();
    m7.mappings.read = F.mappingRow("read", 2, { problems: ["entities[0].source: list_items is not an approved read tool"] });
    m7.answers["PUT /api/admin/connectors/merxa-lite/mappings/read"] = () => jsonResponse(409, { code: "version_conflict", message: "the mapping changed since you loaded it", latest_version: 3 });
    renderM7(<MappingsPage slug="merxa-lite" />);
    expect(await screen.findByText(/no longer passes the tool checks/)).toBeInTheDocument();
    const editor = await screen.findByRole("textbox", { name: "Read mapping (YAML)" });
    await user.click(editor);
    await user.paste("\n# note\n");
    await user.click(screen.getByRole("button", { name: "Save mapping" }));
    expect(await screen.findByText("Someone saved a newer version of this mapping meanwhile.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load the latest version" })).toBeInTheDocument();
  });

  it("action tab: dry run renders the call without executing it and runs the read-back", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<MappingsPage slug="merxa-lite" />);
    await screen.findByRole("textbox", { name: "Read mapping (YAML)" });
    await user.click(screen.getByRole("tab", { name: /Action mapping/ }));
    expect(await screen.findByRole("textbox", { name: "Action mapping (YAML)" })).toBeInTheDocument();
    const dry = screen.getByRole("region", { name: "Dry run" });
    expect(within(dry).getByLabelText(/^Change/)).toHaveValue("transfer_order.create");
    await user.click(within(dry).getByRole("button", { name: "Run dry run" }));
    const result = await waitFor(() => dry.querySelector("[data-dry-run-result]") as HTMLElement);
    expect(result).toHaveTextContent("Not executed");
    expect(result).toHaveTextContent("would call create_transfer_order");
    expect(result).toHaveTextContent("3 records found; none matches this change yet.");
    expect(calls.find((c) => c.url.endsWith("/dry-run"))!.body).toMatchObject({ op: "transfer_order.create", run_readback: true, target_ref: null, expected_version: null });
  });
});

describe("Connector detail: crawl snapshot status", () => {
  it("crawled badge, as-of range, pages, truncated entities, last failure and 'Crawl now'", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderM7(<ConnectorDetail slug="merxa-lite" />);
    const section = await screen.findByRole("region", { name: "Crawl snapshot" });
    expect(await within(section).findByText("Crawled")).toBeInTheDocument();
    expect(section).toHaveTextContent("8 Oct 2026, 09:12 UTC – 8 Oct 2026, 09:19 UTC");
    expect(section).toHaveTextContent("113");
    expect(within(section).getByRole("note")).toHaveTextContent("Stopped at the page limit for inventory_position");
    expect(within(section).getByRole("table", { name: "Rows per entity" })).toHaveTextContent("9,800");
    expect(within(section).queryByRole("alert")).toBeNull(); // the failure is older than the current snapshot
    await expectNoA11yViolations(container);
    await user.click(within(section).getByRole("button", { name: "Crawl now" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/admin/connectors/merxa-lite/sync")).toBe(true));
    expect(toastSpy.calls.some((t) => t.title === "Crawl queued")).toBe(true);
    // A queued crawl (trigger state due, nothing loading yet) is shown until the worker starts it.
    expect(await within(section).findByText(/Crawl queued \(/)).toHaveAttribute("role", "status");
  });

  it("a crawl in progress is announced and a newer failure is shown", async () => {
    m7.snapshots = F.snapshots({
      items: [
        { id: "snap-3", status: "loading", created_at: "2026-10-08T09:50:00+00:00", row_counts: {}, consistency: "crawled" },
        { id: "snap-x", status: "failed", error: "too_many_rows: the crawl exceeds 2000000 rows", created_at: "2026-10-08T09:40:00+00:00", row_counts: {} },
      ],
    });
    renderM7(<ConnectorDetail slug="merxa-lite" />);
    const section = await screen.findByRole("region", { name: "Crawl snapshot" });
    expect(await within(section).findByText(/Crawling since/)).toHaveAttribute("role", "status");
    expect(within(section).getByRole("alert")).toHaveTextContent("too_many_rows");
    expect(within(section).getByRole("button", { name: "Crawling…" })).toBeInTheDocument();
  });
});

// ───────────────────────────── §5 signals ─────────────────────────────

describe("signals (lib)", () => {
  it("sources keep the hub's title and licence; a hub outage is a message, not an error", () => {
    const list = signalSourceListSchema.parse(F.signalSources());
    expect(list.sources.map((s) => [s.id, s.name, s.licence])).toEqual([
      ["gdacs", "GDACS disaster alerts", "CC BY 4.0"],
      ["usgs", "usgs", "Public domain (USGS)"],
    ]);
    expect(signalSourceListSchema.parse({ items: [], hub_error: "signal_hub_unreachable" }).hubError).toBe("signal_hub_unreachable");
    expect(hubErrorText("signal_hub_not_configured")).toMatch(/not configured/);
    expect(hubErrorText(null)).toBeNull();
  });

  it("watch regions: engine shapes, GeoJSON rings closed, only the fields of the kind are sent", () => {
    const regions = watchRegionListSchema.parse(F.watchRegions());
    expect(regions.map(regionSummary)).toEqual(["150 km around port VNSGN", "Polygon with 4 points"]);
    expect(regionBody(regions[0]!)).toEqual({ id: "cat-lai", name: "Cat Lai port area", kind: "radius", enabled: true, node: { type: "port", code: "VNSGN" }, radius_km: 150 });
    expect(Object.keys(regionBody(regions[1]!))).toEqual(["id", "name", "kind", "enabled", "polygon"]);
    const ring = parsePolygon("80, 10\n95 10\n95;22");
    expect(ring).toEqual({ ring: [[80, 10], [95, 10], [95, 22], [80, 10]] });
    expect(parsePolygon("80, 10\nnope")).toEqual({ error: 'Line 2: write "longitude, latitude".' });
    expect(parsePolygon('{"type":"Polygon","coordinates":[[[1,1],[2,2],[1,1]]]}')).toEqual({ error: "Enter at least three different points." });
    expect(parsePolygon("200, 10\n1,1\n2,2")).toHaveProperty("error");
    const id = regionIdFromName("A".repeat(80), ["a".repeat(32)]);
    expect(id).toMatch(/^[a-z][a-z0-9_-]{0,39}$/);
    expect(id).toBe(`${"a".repeat(32)}-2`);
    const bad = { id: "x", name: "x", kind: "radius", node: { type: "lane", code: "L1" }, radius_km: 5 } as unknown as WatchRegion;
    expect(watchRegionListSchema.parse([bad])).toEqual([]);
  });
});

describe("Signals page", () => {
  it("sources: title, licence, cadence; subscribing PUTs {enabled} and reports a hub that lags; axe", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderM7(<SignalsPage />);
    const table = await screen.findByRole("table", { name: "Signal sources" });
    const gdacs = within(table).getByRole("rowheader", { name: /GDACS disaster alerts/ }).closest("tr")!;
    expect(gdacs).toHaveTextContent("CC BY 4.0");
    expect(within(gdacs).getByLabelText("Polling cadence of GDACS disaster alerts")).toHaveValue("900");
    const usgs = within(table).getByRole("rowheader", { name: /usgs/ }).closest("tr")!;
    expect(usgs).toHaveTextContent("3 failures in a row");
    await expectNoA11yViolations(container);
    await user.click(within(usgs).getByRole("switch", { name: "Subscribe to usgs" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/admin/signals/sources/usgs")).toBe(true));
    expect(calls.find((c) => c.url === "/api/admin/signals/sources/usgs")!.body).toEqual({ enabled: true });
    await waitFor(() => expect(toastSpy.calls.some((t) => t.title === "usgs: subscribed" && /doesn't support this setting yet/.test(String((t.opts as { description?: string }).description)))).toBe(true));
  });

  it("cadence (§5): choosing one PUTs only {cadence_s}; 'Default' sends null; out-of-range values are never offered", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<SignalsPage />);
    const table = await screen.findByRole("table", { name: "Signal sources" });
    const gdacs = within(table).getByRole("rowheader", { name: /GDACS disaster alerts/ }).closest("tr")!;
    const select = within(gdacs).getByLabelText("Polling cadence of GDACS disaster alerts");
    const values = [...select.querySelectorAll("option")].map((o) => o.getAttribute("value"));
    expect(values[0]).toBe("");
    expect(values.slice(1).every((v) => Number(v) >= 60 && Number(v) <= 86_400)).toBe(true);
    await user.selectOptions(select, "3600");
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/admin/signals/sources/gdacs")).toBe(true));
    expect(calls.filter((c) => c.url === "/api/admin/signals/sources/gdacs").at(-1)!.body).toEqual({ cadence_s: 3600 });
    await waitFor(() => expect(select).toHaveValue("3600"));
    await user.selectOptions(select, "");
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/admin/signals/sources/gdacs")).toHaveLength(2));
    expect(calls.filter((c) => c.url === "/api/admin/signals/sources/gdacs").at(-1)!.body).toEqual({ cadence_s: null });
    expect(cadenceFromOption("30")).toBeUndefined();
    expect(cadenceFromOption("90000")).toBeUndefined();
    expect(cadenceOptions({ cadence_s: 120, default_cadence_s: 900 }).map((o) => o.label)).toContain("every 2 min");
    expect(networkCodesSchema.parse({ nodes: { dc: ["DC-1", 4, "bad code"], lane: ["L1"], nope: ["x"] } })).toEqual({ dc: ["DC-1"] });
  });

  it("watch regions: add a radius around a named site; the PUT carries node {type, code} and radius_km", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<SignalsPage />);
    const form = await screen.findByRole("form", { name: "Add a watch region" });
    expect(screen.getByRole("table", { name: "Watch regions" })).toHaveTextContent("150 km around port VNSGN");
    await user.click(within(form).getByRole("button", { name: "Add region" }));
    expect(within(form).getByText("Name the region.")).toBeInTheDocument();
    expect(within(form).getByText("Enter the site code.")).toBeInTheDocument();
    await user.type(within(form).getByLabelText(/^Name/), "Rotterdam DC");
    await user.selectOptions(within(form).getByLabelText("Site type"), "dc");
    // The network's DC codes are offered (GET /signals/network); typing stays possible.
    const codeInput = within(form).getByLabelText(/^Site code/);
    await waitFor(() => expect(codeInput.getAttribute("list")).toBeTruthy());
    const options = [...document.getElementById(codeInput.getAttribute("list")!)!.querySelectorAll("option")].map((o) => o.getAttribute("value"));
    expect(options).toEqual(["DC-1", "DC-2"]);
    await user.type(codeInput, "DC-1");
    await user.clear(within(form).getByLabelText(/^Radius/));
    await user.type(within(form).getByLabelText(/^Radius/), "80");
    await user.click(within(form).getByRole("button", { name: "Add region" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/admin/signals/watch-regions")).toBe(true));
    const regions = (calls.find((c) => c.method === "PUT")!.body as { regions: Record<string, unknown>[] }).regions;
    expect(regions).toHaveLength(3);
    expect(regions[2]).toEqual({ id: "rotterdam-dc", name: "Rotterdam DC", kind: "radius", enabled: true, node: { type: "dc", code: "DC-1" }, radius_km: 80 });
    expect(regions[1]).toMatchObject({ polygon: { type: "Polygon" } });
    expect(regions[1]).not.toHaveProperty("node");
  });

  it("alert rules: edit and save; defaults are labelled", async () => {
    const user = userEvent.setup();
    const { calls } = renderM7(<SignalsPage />);
    expect(await screen.findByText("Using the default rules. Saving a change stores your own.")).toBeInTheDocument();
    const rule = document.querySelector('[data-alert-rule="severe"]') as HTMLElement;
    const sev = within(rule).getByLabelText("Minimum severity (%)");
    await user.clear(sev);
    await user.type(sev, "80");
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/admin/signals/alert-rules")).toBe(true));
    const sent = (calls.find((c) => c.url === "/api/admin/signals/alert-rules" && c.method === "PUT")!.body as { rules: { when: { min_severity_bp: number } }[] }).rules;
    expect(sent[0]!.when.min_severity_bp).toBe(8000);
  });

  it("the demo pack launcher is DEMO + presenter only", async () => {
    const { unmount } = renderM7(<SignalsPage />);
    await screen.findByRole("table", { name: "Signal sources" });
    expect(screen.queryByRole("region", { name: "Demo scenarios" })).toBeNull();
    unmount();
    renderM7(<SignalsPage />, AVA());
    expect(await screen.findByRole("region", { name: "Demo scenarios" })).toBeInTheDocument();
    await settle();
  });
});

// ───────────────────────────── §6 audit & evals ─────────────────────────────

describe("audit activity", () => {
  it("lib: day bounds, category tagging and detail lines", () => {
    expect(dayBounds("2026-10-07", "2026-10-08")).toEqual({ since: "2026-10-07T00:00:00+00:00", until: "2026-10-09T00:00:00+00:00" });
    expect(dayBounds("", "bad")).toEqual({});
    const page = activityPageSchema.parse({ items: [...F.activity(), { nope: 1 }], next_before: null });
    expect(page.items.map((i) => i.category)).toEqual(["admin", "approvals", "runs", "change_sets", "signals"]);
    expect(page.items.map(activityDetail)).toEqual([null, "as approver · plan p2 · “Looks right”", "chat · succeeded · spec v3", "merxa · 4 lines · CS-104", "opened automatically"]);
    expect(activityQuery({ category: "runs", actor: " sam ", caseId: "", action: "run.", from: "2026-10-07", to: "" })).toEqual({ limit: 100, categories: ["runs"], actor: "sam", case_id: undefined, action: "run.", since: "2026-10-07T00:00:00+00:00" });
  });

  it("page: one timeline across categories, server-side filters, case links, admin diffs, cursor paging", async () => {
    const user = userEvent.setup();
    base.activity = F.activity();
    const { calls, container } = renderM7(<AuditPage />);
    const table = (await screen.findByRole("table", { name: "Activity" })) as HTMLTableElement;
    expect(table.tBodies[0]!.rows).toHaveLength(5);
    expect(within(table).getByRole("rowheader", { name: "approval.approve" }).closest("tr")).toHaveTextContent("Approvals");
    expect(within(table).getAllByRole("link", { name: /open case 019a0c4e-6f1e-7c8a-9d55-0b1c2d3e4f50/ })[0]).toHaveAttribute("href", "/cases/019a0c4e-6f1e-7c8a-9d55-0b1c2d3e4f50");
    expect(within(table).getByText("Show changes")).toBeInTheDocument();
    expect(calls.find((c) => c.url.startsWith("/api/admin/audit/activity"))!.url).toBe("/api/admin/audit/activity?limit=100");
    await expectNoA11yViolations(container);

    const filters = screen.getByRole("search", { name: "Filter the activity" });
    await user.selectOptions(within(filters).getByLabelText("Category"), "approvals");
    await user.type(within(filters).getByLabelText(/^Username/), "nora");
    await user.type(within(filters).getByLabelText("From day (UTC)"), "2026-10-08");
    await user.click(within(filters).getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect((screen.getByRole("table", { name: "Activity" }) as HTMLTableElement).tBodies[0]!.rows).toHaveLength(1));
    expect(calls.at(-1)!.url).toBe("/api/admin/audit/activity?limit=100&category=approvals&actor=nora&since=2026-10-08T00%3A00%3A00%2B00%3A00");
    expect(calls.every((c) => !c.url.includes("tenant="))).toBe(true);
  });

  it("'Load older activity' sends the opaque cursor", async () => {
    const user = userEvent.setup();
    base.activity = Array.from({ length: 103 }, (_, i) => ({ key: `run:${1000 - i}`, category: "runs", at: new Date(Date.UTC(2026, 9, 8, 9, 0, 0) - i * 60_000).toISOString(), actor: "priya", action: "run.chat.succeeded", target: null, case_id: null, detail: {} }));
    const { calls } = renderM7(<AuditPage />);
    expect(await screen.findByText("100 entries (more available)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load older activity" }));
    expect(await screen.findByText("103 entries")).toBeInTheDocument();
    expect(decodeURIComponent(calls.at(-1)!.url)).toContain("before=2026-10-08T07:21:00.000Z|run:901");
  });
});

describe("evals", () => {
  it("lib: the engine's per-case counts and scores; unreadable files are kept with their error", () => {
    const list = evalsResponseSchema.parse(F.evals());
    expect(list.directoryAvailable).toBe(true);
    expect(list.runs.map((r) => r.key)).toEqual(["20261008T080000Z.json", "20261007T080000Z.json"]);
    const [pass, fail] = list.runs[0]!.cases;
    expect(caseScore(pass!)).toMatchObject({ passed: 12, total: 12 });
    expect(caseScore(fail!)).toMatchObject({ passed: 6, total: 8 });
    expect(summaryTiles(list.runs[0]!).map((t) => `${t.label}: ${t.value}`)).toEqual(["Cases: 2", "Passed: 1", "Failed: 1", "Checks passed: 18 / 20", "Numeric grounding: 95 %"]);
    expect(list.runs[1]!.error).toBe("unreadable: JSONDecodeError");
    expect(evalsResponseSchema.parse({ items: [], directory_available: false }).directoryAvailable).toBe(false);
  });

  // M8 (128): the listed files are the Evals page's "Results folder" (runs, history and drill-down
  // are covered in m8-evals.test.tsx).
  it("results folder: per-file case counts; unreadable files keep their error", async () => {
    const { container } = renderM7(<EvalResultsFolder refreshKey="0" onImported={() => {}} />, AVA());
    const table = await screen.findByRole("table", { name: "Results files" });
    const ok = within(table).getByRole("rowheader", { name: "20261008T080000Z.json" }).closest("tr")!;
    expect(ok).toHaveTextContent("1 / 2 passed · 20 checks");
    expect(within(ok).getByRole("button", { name: /Import/ })).toBeInTheDocument();
    const bad = within(table).getByRole("rowheader", { name: "20261007T080000Z.json" }).closest("tr")!;
    expect(bad).toHaveTextContent("unreadable: JSONDecodeError");
    expect(within(bad).queryByRole("button", { name: /Import/ })).toBeNull();
    await expectNoA11yViolations(container);
  });

  it("no results folder mounted → says so", async () => {
    m7.evals = { items: [], directory_available: false };
    renderM7(<EvalResultsFolder refreshKey="0" onImported={() => {}} />, AVA());
    expect(await screen.findByText(/isn't mounted into the engine/)).toBeInTheDocument();
  });
});
