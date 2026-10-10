/**
 * M8 mapping compiler + proposed mappings in the admin (`docs/contracts/121`): the proposal panel
 * (disabled states, AI fallback banner, confidence as text, origins, rationales rendered inert),
 * open-in-editor YAML with confidence comments (missing required columns → editor diagnostics),
 * saving with `proposal_id` (action → 202 change request + Pending changes link), Explain (deduplicated
 * reads, SQL on demand), the staged "last crawl" preview (disabled without staged data, FK check
 * examples), the connector call to action, the expression checker. Engine shapes from
 * `routes_admin_mappings.py`; fixtures are checked against the generated contracts.
 */
import {
  adminChangeRequestSchema,
  confidencePercent,
  mappingPlanSchema,
  mappingPreviewSchema,
  mappingProposalSchema,
} from "@keelson/contracts";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AdminShell } from "@/components/admin/AdminShell";
import { ConnectorDetail } from "@/components/admin/ConnectorDetail";
import { MappingsPage } from "@/components/admin/MappingsPage";
import { checkMapping, expressionProblem, readPreviewSchema } from "@/lib/admin/mappings";
import {
  actionDraftText,
  commentText,
  defaultSelection,
  proposalHeadline,
  proposalListSchema,
  proposalSchema,
  proposerStatusSchema,
  readDraftText,
} from "@/lib/admin/proposals";
import { expectNoA11yViolations } from "./a11y";
import { adminRoutes, makeAdminServer, type AdminServer } from "./adminMock";
import * as F7 from "./fixtures/m7";
import * as F from "./fixtures/m8-mappings";
import { fakeApi, jsonResponse, makeMe, withSession, type Route } from "./session";

const nav = vi.hoisted(() => ({ pathname: "/admin/connectors/merxa-lite/mappings", push: vi.fn() }));
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

vi.mock("@/components/admin/lazy", () => ({
  LazyCodeEditor: ({ value, onChange, label, describedBy }: { value: string; onChange: (v: string) => void; label: string; describedBy?: string }) => (
    <textarea aria-label={label} aria-describedby={describedBy} value={value} onChange={(e) => onChange(e.target.value)} />
  ),
  LazyWorkflowGraph: () => <div data-testid="workflow-graph" />,
}));

vi.setConfig({ testTimeout: 30_000 });

const SAM = () => makeMe({ sub: F7.SUB_SAM, username: "sam", name: "Sam Okafor", roles: ["tenant-admin", "planner"], tenants: ["demo-retail"] });

/** The foreign ERP's tools plus its get-by-key read tool (the proposed read-back). */
const TOOLS = () => [
  ...F7.liteTools(),
  { name: "get_item", title: "Get item", access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none", status: "approved", binding: "auto" },
];

type M8Server = {
  connector: Record<string, unknown>;
  mappings: { read: Record<string, unknown>; action: Record<string, unknown> };
  staged: Record<string, unknown> | null;
  proposer: Record<string, unknown>;
  proposals: Record<string, unknown>[];
  /** `METHOD /path` → answer, checked first. */
  answers: Record<string, () => Response>;
};

let base: AdminServer;
let m8: M8Server;

const bodyOf = (init: RequestInit): Record<string, unknown> => (init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {});

function m8Routes(s: M8Server): Route {
  return (url, init) => {
    const method = init.method ?? "GET";
    const path = url.split("?")[0]!;
    const custom = s.answers[`${method} ${path}`];
    if (custom) return custom();
    if (!path.startsWith("/api/admin/")) return undefined;
    const p = path.slice("/api/admin".length);
    let r: RegExpExecArray | null;
    if (method === "GET" && p === "/mappings/proposer") return jsonResponse(200, s.proposer);
    if (p === "/connectors/merxa-lite" && method === "GET") return jsonResponse(200, s.connector);
    if (method === "GET" && p === "/connectors/merxa-lite/tools") return jsonResponse(200, { items: TOOLS() });
    if (method === "GET" && p === "/connectors/merxa-lite/events") return jsonResponse(200, { items: [] });
    if (method === "GET" && p === "/connectors/merxa-lite/capabilities") return jsonResponse(200, F7.capabilities());
    if (method === "GET" && p === "/connectors/merxa-lite/snapshots") return jsonResponse(200, F7.snapshots());
    if (method === "GET" && p === "/connectors/merxa-lite/staged") return s.staged ? jsonResponse(200, s.staged) : jsonResponse(404, { code: "no_staged_data", message: "no crawl of this connector has staged data yet" });
    if ((r = /^\/connectors\/merxa-lite\/mappings\/(read|action)$/.exec(p))) {
      const kind = r[1] as "read" | "action";
      if (method === "GET") return jsonResponse(200, s.mappings[kind]);
      if (method === "PUT") {
        const b = bodyOf(init);
        const current = s.mappings[kind]["version"] as number | null;
        if (b["base_version"] !== current) return jsonResponse(409, { code: "version_conflict", message: "the mapping changed since you loaded it", latest_version: current });
        if (kind === "action" && b["proposal_id"]) return jsonResponse(202, F.mappingChangeRequest());
        const version = (current ?? 0) + 1;
        s.mappings[kind] = { ...F7.mappingRow(kind, version), mapping: b["mapping"], origin: b["proposal_id"] ? "proposal" : "manual" };
        return jsonResponse(201, { ...s.mappings[kind], unchanged: false });
      }
    }
    if (method === "POST" && p === "/connectors/merxa-lite/mappings/read/compile") return jsonResponse(200, F.plan());
    if (method === "POST" && p === "/connectors/merxa-lite/mappings/read/preview") return jsonResponse(200, F.stagedPreview());
    if (method === "POST" && p === "/connectors/merxa-lite/mappings/read/test") return jsonResponse(200, F7.readPreview());
    if (p === "/connectors/merxa-lite/mappings/proposals") {
      if (method === "GET") return jsonResponse(200, { items: s.proposals });
      if (method === "POST") return jsonResponse(201, F.proposal());
    }
    if ((r = /^\/connectors\/merxa-lite\/mappings\/proposals\/([^/]+)(\/discard)?$/.exec(p))) {
      if (r[2] && method === "POST") return jsonResponse(200, F.proposal({ id: r[1], status: "discarded" }));
      if (method === "GET") return jsonResponse(200, F.proposal({ id: r[1], status: r[1] === F.OLDER_PROPOSAL_ID ? "discarded" : "ready" }));
    }
    return undefined;
  };
}

function renderM8(ui: ReactNode, me = SAM()) {
  const { api, calls } = fakeApi([m8Routes(m8), ...adminRoutes(base)]);
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

beforeEach(() => {
  base = makeAdminServer();
  base.connectors = [...base.connectors, F7.liteConnector()];
  m8 = {
    connector: F7.liteConnector(),
    mappings: { read: F7.mappingRow("read", 2), action: F7.mappingRow("action", 1) },
    staged: F.stagedSet(),
    proposer: F.proposerStatus(),
    proposals: [F.proposalSummary()],
    answers: {},
  };
  nav.pathname = "/admin/connectors/merxa-lite/mappings";
  nav.push.mockReset();
  toastSpy.calls = [];
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(F7.NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("M8 fixtures match the generated contracts", () => {
  it("proposal, plan, staged preview and the connector_mapping change request", () => {
    expect(mappingProposalSchema.parse(F.proposal()).read?.entities).toHaveLength(2);
    expect(mappingPlanSchema.parse(F.plan()).sources).toHaveLength(2);
    expect(mappingPreviewSchema.parse(F.stagedPreview()).checks?.[0]?.examples).toEqual(["dc:DE1", "dc:NL1"]);
    expect(adminChangeRequestSchema.parse(F.mappingChangeRequest()).kind).toBe("connector_mapping");
    expect(proposerStatusSchema.parse(F.proposerStatus()).llm.available).toBe(false);
    expect(proposalListSchema.parse({ items: [F.proposalSummary(), { bad: 1 }] })).toHaveLength(1);
    expect(readPreviewSchema.parse(F.stagedPreview()).entities[0]?.columns).toHaveLength(2);
  });
});

describe("proposals (lib)", () => {
  const p = proposalSchema.parse(F.proposal());

  it("headline says why the AI step did not run", () => {
    expect(proposalHeadline(p)).toBe("AI unavailable: rule-based suggestions.");
    expect(proposalHeadline({ ...p, proposer: "llm+heuristic", model_alias: "fast-default", notes: [] })).toBe("Rules refined by the AI (fast-default).");
    expect(proposalHeadline({ ...p, notes: [] })).toBe("Rule-based suggestions (no AI step).");
  });

  it("read draft YAML: a comment per field, nulls for missing required columns, used lookups only", () => {
    const sel = defaultSelection(p);
    expect([...sel.entities]).toEqual([0]); // the incomplete allocation mapping is not selected by default
    const text = readDraftText(p, sel)!;
    expect(text).toContain("code: $.code # 80% · Rules · name matches code");
    expect(text).toContain("kind: \"'store'\" # 90% · Rules + AI");
    expect(text).not.toContain("lookups:");
    expect(text).not.toContain("\n<img"); // the rationale stays inside a comment
    expect(checkMapping("read", text, TOOLS() as never).value).not.toBeNull();
    const both = readDraftText(p, { ...sel, entities: new Set([0, 1]) })!;
    expect(both).toContain("qty: null # required: nothing in the sample fits");
    expect(both).toContain("lookups:");
    const checked = checkMapping("read", both, TOOLS() as never);
    expect(checked.value).toBeNull();
    expect(checked.diagnostics.some((d) => d.severity === "error" && d.message.includes("qty"))).toBe(true);
    expect(readDraftText(p, { ...sel, entities: new Set() })).toBeNull();
  });

  it("action draft YAML and comment hygiene", () => {
    const text = actionDraftText(p, defaultSelection(p))!;
    expect(text).toMatch(/# 30% · Rules · generic update of the allocation record · generic update tool/);
    expect(checkMapping("action", text, TOOLS() as never).value).not.toBeNull();
    expect(commentText("line one\nline two three\u0000")).toBe("line one line two three");
    expect(commentText("x".repeat(500))).toHaveLength(160);
    expect(confidencePercent(8599)).toBe("85%");
  });

  it("expressions: M8 functions anywhere, aggregates only whole and only when grouped", () => {
    expect(expressionProblem("field('style', $.style_code, '$.family')")).toBeNull();
    expect(expressionProblem("cents($.price) ")).toBeNull();
    expect(expressionProblem("upper(trim(lower($.x)))")).toBeNull();
    expect(expressionProblem("iso_week($.d)")).toBeNull();
    expect(expressionProblem("first($.x)")).toMatch(/aggregate/);
    expect(expressionProblem("first($.x)", { aggregates: true })).toBeNull();
    expect(expressionProblem("count()", { aggregates: true })).toBeNull();
    expect(expressionProblem("int(first($.x))", { aggregates: true })).toMatch(/never nested/);
    expect(expressionProblem("first($.x) + 'a'", { aggregates: true })).toMatch(/never nested/);
    expect(expressionProblem("min($.a) + max($.b)", { aggregates: true })).toMatch(/never nested/);
  });
});

describe("Mappings page: proposals", () => {
  it("panel: AI unavailable is said and disabled, confidence as text, origins, rationale inert; axe", async () => {
    const user = userEvent.setup();
    const { container, calls } = renderM8(<MappingsPage slug="merxa-lite" propose />);
    const panel = await waitFor(() => container.querySelector("[data-proposal-panel]") as HTMLElement);
    const ai = await within(panel).findByRole("checkbox", { name: /Refine with AI/ });
    await waitFor(() => expect(ai).toBeDisabled());
    expect(panel).toHaveTextContent("No AI model is configured for this tenant");
    expect(within(panel).getByRole("checkbox", { name: /Sample the read tools/ })).not.toBeDisabled();
    await user.click(within(panel).getByRole("button", { name: "Propose mappings" }));
    const result = await waitFor(() => panel.querySelector("[data-proposal]") as HTMLElement);
    expect(calls.find((c) => c.method === "POST" && c.url.endsWith("/mappings/proposals"))!.body).toEqual({ kinds: ["read", "action"], proposer: "heuristic", sample: true });
    expect(within(result).getByText("AI unavailable: rule-based suggestions.")).toBeInTheDocument();
    const store = result.querySelector('[data-proposed-entity="0"]') as HTMLElement;
    expect(store).toHaveTextContent("76%");
    expect(within(store).getAllByText("Rules + AI").length).toBeGreaterThan(0);
    expect(within(store).getByText("AI")).toBeInTheDocument();
    // the model's rationale is text, never markup
    expect(store.querySelector("img")).toBeNull();
    expect(within(store).getByText(F.XSS)).toBeInTheDocument();
    expect(store).toHaveTextContent("upper($.name)");
    const alloc = result.querySelector('[data-proposed-entity="1"]') as HTMLElement;
    expect(alloc.querySelector('[data-missing-column="qty"]')).not.toBeNull();
    expect(within(alloc).getByRole("checkbox", { name: "allocation" })).not.toBeChecked();
    expect(result.querySelector('[data-proposed-op="allocation.set_qty"]')).toHaveTextContent("Generic update tool");
    await expectNoA11yViolations(panel);
  });

  it("an inactive connector proposes from the tool catalogue only", async () => {
    m8.connector = F7.liteConnector({ status: "disabled" });
    m8.proposer = F.proposerStatus({ llm: { available: true, reason: null, message: null } });
    const user = userEvent.setup();
    const { container, calls } = renderM8(<MappingsPage slug="merxa-lite" propose />);
    const panel = await waitFor(() => container.querySelector("[data-proposal-panel]") as HTMLElement);
    const sample = within(panel).getByRole("checkbox", { name: /Sample the read tools/ });
    await waitFor(() => expect(sample).toBeDisabled());
    expect(panel).toHaveTextContent("Activate the connector to sample it");
    expect(within(panel).getByRole("checkbox", { name: /Refine with AI/ })).toBeChecked();
    await user.click(within(panel).getByRole("button", { name: "Propose mappings" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/proposals"))).toBe(true));
    expect(calls.find((c) => c.method === "POST" && c.url.endsWith("/proposals"))!.body).toMatchObject({ proposer: "auto", sample: false });
  });

  it("open in editor → YAML with confidence comments → save sends proposal_id", async () => {
    const user = userEvent.setup();
    const { container, calls } = renderM8(<MappingsPage slug="merxa-lite" propose />);
    const panel = await waitFor(() => container.querySelector("[data-proposal-panel]") as HTMLElement);
    await user.click(within(panel).getByRole("button", { name: "Propose mappings" }));
    await waitFor(() => panel.querySelector("[data-proposal]"));
    await user.click(within(panel).getByRole("checkbox", { name: "allocation" }));
    await user.click(within(panel).getByRole("button", { name: "Open read draft in editor" }));
    const editor = (await screen.findByRole("textbox", { name: "Read mapping (YAML)" })) as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toContain("# 80% · Rules · name matches code"));
    expect(editor.value).toContain("qty: null");
    expect(container.querySelector("[data-proposal-draft]")).not.toBeNull();
    expect(await screen.findByText(/problem(s)? to fix before saving/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save mapping" })).toHaveAttribute("aria-disabled", "true");
    // fill the missing column: valid → saved with the proposal id
    await user.click(editor);
    const fixed = editor.value.replace(/qty: null[^\n]*/, "qty: int($.qty)");
    await user.clear(editor);
    await user.click(editor);
    await user.paste(fixed);
    expect(await screen.findByText(/Valid against the mapping contract|Valid ·/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save mapping" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toBe("/api/admin/connectors/merxa-lite/mappings/read");
    expect(put.body).toMatchObject({ base_version: 2, proposal_id: F.PROPOSAL_ID });
    expect(JSON.stringify((put.body as { mapping: unknown }).mapping)).toContain('"qty":"int($.qty)"');
  });

  it("an action draft goes to a second admin: 202 → banner + Pending changes link", async () => {
    const user = userEvent.setup();
    const { container, calls } = renderM8(<MappingsPage slug="merxa-lite" propose />);
    const panel = await waitFor(() => container.querySelector("[data-proposal-panel]") as HTMLElement);
    await user.click(within(panel).getByRole("button", { name: "Propose mappings" }));
    await waitFor(() => panel.querySelector("[data-proposal]"));
    await user.click(within(panel).getByRole("button", { name: "Open action draft in editor" }));
    const editor = (await screen.findByRole("textbox", { name: "Action mapping (YAML)" })) as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toContain("generic update tool"));
    const banner = container.querySelector("[data-second-admin]") as HTMLElement;
    expect(banner).toHaveTextContent("Requires a second admin");
    expect(banner).toHaveTextContent("Saves an action mapping proposed by rules or AI");
    await user.click(screen.getByRole("button", { name: "Send for approval" }));
    const sent = await waitFor(() => container.querySelector("[data-change-request]") as HTMLElement);
    expect(sent).toHaveTextContent("Sent for approval");
    expect(within(sent).getByRole("link", { name: "Open Pending changes" })).toHaveAttribute("href", `/admin/changes?request=${F.CR_MAPPING}`);
    expect(calls.find((c) => c.method === "PUT")!.body).toMatchObject({ base_version: 1, proposal_id: F.PROPOSAL_ID });
    expect(toastSpy.calls.some((t) => t.title === "Sent for approval")).toBe(true);
  });

  it("earlier proposals load; discard", async () => {
    const user = userEvent.setup();
    const { container, calls } = renderM8(<MappingsPage slug="merxa-lite" propose />);
    const panel = await waitFor(() => container.querySelector("[data-proposal-panel]") as HTMLElement);
    const earlier = await within(panel).findByRole("combobox", { name: "Earlier proposals" });
    await user.selectOptions(earlier, F.OLDER_PROPOSAL_ID);
    const shown = await waitFor(() => panel.querySelector(`[data-proposal="${F.OLDER_PROPOSAL_ID}"]`) as HTMLElement);
    expect(shown).toHaveTextContent("Discarded");
    expect(within(shown).getByRole("button", { name: "Open read draft in editor" })).toHaveAttribute("aria-disabled", "true");
    await user.click(within(panel).getByRole("button", { name: "Propose mappings" }));
    const fresh = await waitFor(() => panel.querySelector(`[data-proposal="${F.PROPOSAL_ID}"]`) as HTMLElement);
    await user.click(within(fresh).getByRole("button", { name: "Discard proposal" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/discard"))).toBe(true));
    expect(await within(panel).findByText("Discarded")).toBeInTheDocument();
  });

  it("errors are shown (page states)", async () => {
    m8.answers["POST /api/admin/connectors/merxa-lite/mappings/proposals"] = () => jsonResponse(429, { code: "proposal_busy", message: "a mapping proposal is already running; try again when it finished" });
    const user = userEvent.setup();
    const { container } = renderM8(<MappingsPage slug="merxa-lite" propose />);
    const panel = await waitFor(() => container.querySelector("[data-proposal-panel]") as HTMLElement);
    await user.click(within(panel).getByRole("button", { name: "Propose mappings" }));
    expect(await within(panel).findByText(/already running/)).toBeInTheDocument();
  });
});

describe("Mappings page: explain and staged preview", () => {
  it("explain lists the deduplicated reads and reveals SQL on demand", async () => {
    const user = userEvent.setup();
    const { container } = renderM8(<MappingsPage slug="merxa-lite" />);
    await screen.findByRole("textbox", { name: "Read mapping (YAML)" });
    await user.click(screen.getByRole("button", { name: "Explain" }));
    const plan = await waitFor(() => container.querySelector("[data-mapping-plan]") as HTMLElement);
    expect(plan).toHaveTextContent("2 distinct reads for 3 uses (each read is fetched once)");
    expect(plan.querySelectorAll("[data-plan-source]")).toHaveLength(2);
    expect(plan).toHaveTextContent("entities[0] (location), entities[1] (location)");
    const grouped = plan.querySelector('[data-plan-entity="entities[1] (location)"]') as HTMLDetailsElement;
    expect(grouped).toHaveTextContent("one row per $.cluster_code");
    const sql = within(grouped).getByText("Show SQL").closest("details")!;
    expect(sql.open).toBe(false);
    await user.click(within(grouped).getByText("entities[1] (location)"));
    await user.click(within(grouped).getByText("Show SQL"));
    expect(sql.open).toBe(true);
    expect(within(grouped).getByLabelText("Generated SQL of entities[1] (location)")).toHaveTextContent("arg_min");
    expect(plan).toHaveTextContent("Stops the crawl");
    await expectNoA11yViolations(plan);
  });

  it("staged preview: no vendor call, column statistics and FK examples", async () => {
    const user = userEvent.setup();
    const { container, calls } = renderM8(<MappingsPage slug="merxa-lite" />);
    await screen.findByRole("textbox", { name: "Read mapping (YAML)" });
    expect(await screen.findByText(/Last crawl staged .*25 records/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Preview last crawl" }));
    const preview = await waitFor(() => container.querySelector("[data-read-preview]") as HTMLElement);
    expect(calls.find((c) => c.url.endsWith("/read/preview"))!.body).toEqual({ source: "staged" });
    expect(preview).toHaveTextContent("against the last crawl");
    expect(preview.querySelector('[data-preview-check="fk"]')).toHaveTextContent("e.g. dc:DE1, dc:NL1");
    expect(preview.querySelector('[data-preview-column="src_version"]')).toHaveTextContent("33%");
    expect(preview.querySelector('[data-preview-column="src_version"]')).toHaveTextContent("first: item 2: int()");
    expect(calls.some((c) => c.url.endsWith("/read/test"))).toBe(false);
  });

  it("without staged data the staged preview is unavailable and says why", async () => {
    m8.staged = null;
    renderM8(<MappingsPage slug="merxa-lite" />);
    await screen.findByRole("textbox", { name: "Read mapping (YAML)" });
    expect(await screen.findByText(/No crawl has staged data yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview last crawl" })).toHaveAttribute("aria-disabled", "true");
  });
});

describe("connector call to action", () => {
  it("a tier-A connector without a read mapping offers proposals", async () => {
    nav.pathname = "/admin/connectors/merxa-lite";
    m8.mappings.read = F7.mappingRow("read", null);
    renderM8(<ConnectorDetail slug="merxa-lite" />);
    // Two loads in sequence (the connector, then the callout's read mapping): wait for each in turn.
    await screen.findByRole("heading", { name: /Northwind ERP/ });
    const note = await screen.findByText(/has no read mapping yet/);
    expect(within(note.closest("[data-propose-callout]") as HTMLElement).getByRole("link", { name: "Propose mappings" })).toHaveAttribute(
      "href",
      "/admin/connectors/merxa-lite/mappings?propose=1",
    );
  });

  it("a connector with a read mapping shows no call to action", async () => {
    nav.pathname = "/admin/connectors/merxa-lite";
    renderM8(<ConnectorDetail slug="merxa-lite" />);
    await screen.findByRole("heading", { name: /Northwind ERP/ });
    await act(async () => {});
    expect(document.querySelector("[data-propose-callout]")).toBeNull();
  });
});
