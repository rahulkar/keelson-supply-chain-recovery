import type { BlockEnvelope } from "@keelson/contracts";
import { getFixture } from "@keelson/viz/fixtures";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AdminShell } from "@/components/admin/AdminShell";
import { PoliciesPage } from "@/components/admin/PoliciesPage";
import { CaseView } from "@/components/case/CaseView";
import { CaseList } from "@/components/cases/CaseList";
import { PersonaSwitcher } from "@/components/shell/PersonaSwitcher";
import { markCaseSeen } from "@/lib/seen";
import { expectNoA11yViolations } from "./a11y";
import { FakeEventSource } from "./fakeEventSource";
import { caseDetail, fakeApi, jsonResponse, makeMe, withSession, type Route } from "./session";

const nav = vi.hoisted(() => ({ pathname: "/admin/policies", push: vi.fn(), refresh: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ push: nav.push, refresh: nav.refresh }) }));
vi.setConfig({ testTimeout: 20_000 });

beforeEach(() => {
  FakeEventSource.reset();
  window.localStorage.clear();
  nav.push.mockReset();
  nav.refresh.mockReset();
  window.history.replaceState({}, "", "/");
});

const fx = (name: string): BlockEnvelope => structuredClone(getFixture(name)!.envelope);

// ───────────────────────────── case screen ─────────────────────────────

const CASE = "case-m5";
const RUN = "run-m5-1";
const APPROVAL = "approval-cs_01a1f4eyes";

function events(blocks: BlockEnvelope[], opts: { interrupt?: string | undefined } = {}): { seq: number; data: unknown }[] {
  let seq = 0;
  const sc = () => ({ tenantId: "demo-retail", caseId: CASE, runId: RUN, seq: ++seq, stage: "applying", prov: "fact", label: "x", agent: null, evidenceRefs: [] });
  const out: { seq: number; data: unknown }[] = [];
  const push = (e: Record<string, unknown>) => {
    const metadata = { sc: sc() };
    out.push({ seq, data: { ...e, timestamp: 1791255602871 + seq, metadata } });
  };
  push({ type: "RUN_STARTED", threadId: CASE, runId: RUN });
  for (const b of blocks) push({ type: "ACTIVITY_SNAPSHOT", messageId: b.id, activityType: "sc.render", content: b, replace: true });
  push({
    type: "RUN_FINISHED",
    threadId: CASE,
    runId: RUN,
    outcome: opts.interrupt
      ? { type: "interrupt", interrupts: [{ id: opts.interrupt, reason: "approval", message: "Approve the changes", responseSchema: {} }] }
      : { type: "success" },
  });
  return out;
}

function renderCase(blocks: BlockEnvelope[], opts: { me?: ReturnType<typeof makeMe>; interrupt?: string; link?: Record<string, unknown>; extra?: Route[] } = {}) {
  const evs = events(blocks, { interrupt: opts.interrupt });
  const last = evs.at(-1)!.seq;
  const snapshot = caseDetail(CASE, {
    last_event_seq: last,
    open_interrupts: opts.interrupt ? [{ id: opts.interrupt, kind: "approval", message: null, payload: {}, response_schema: {} }] : [],
  });
  const { api, calls } = fakeApi([
    ...(opts.extra ?? []),
    (url) => (url === `/api/cases/${CASE}` ? jsonResponse(200, snapshot) : undefined),
    (url) => (url.endsWith("/decision") ? jsonResponse(202, { status: "recorded", run_id: null }) : undefined),
    (url) => (url.endsWith("/actions") ? jsonResponse(202, { run_id: "run-rollback" }) : undefined),
    (url) => (url.includes("/checklists/") ? jsonResponse(200, { step: { seq: 3, status: "done" }, changed: true }) : undefined),
    (url) =>
      url === "/api/connectors/merxa/link"
        ? jsonResponse(200, opts.link ?? { status: "missing", scopes: [], apply_ready: false })
        : undefined,
  ]);
  const Wrapper = withSession(opts.me ?? makeMe(), api);
  const utils = render(
    <Wrapper>
      <CaseView initial={snapshot} streamOptions={{ createEventSource: FakeEventSource.factory }} />
    </Wrapper>,
  );
  const es = FakeEventSource.latest();
  act(() => {
    es.open();
    for (const e of evs) es.emit(JSON.stringify(e.data), String(e.seq));
  });
  return { ...utils, calls };
}

const LENA = () => makeMe({ sub: "user:lena", username: "lena", name: "Lena Vogel", roles: ["compliance"] });
const MARCO = () => makeMe({ sub: "user:marco", username: "marco", name: "Marco Rossi", roles: ["planner", "approver"] });

describe("case screen (M5)", () => {
  it("four-eyes: a compliance co-approver (read role, no Merxa link) can approve; progress is shown", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase([fx("approval_card.four_eyes")], { me: LENA(), interrupt: APPROVAL });
    const card = await screen.findByRole("region", { name: "Approvals" });
    expect(card).toHaveTextContent("1 of 2 approvals: Marco Rossi ✓ · waiting for Compliance");
    const approve = await screen.findByRole("button", { name: "Approve" });
    await waitFor(() => expect(approve).not.toHaveAttribute("aria-disabled", "true"));
    expect(screen.queryByRole("button", { name: /Connect Merxa/ })).toBeNull();
    await user.click(approve);
    await waitFor(() => expect(calls.some((c) => c.url === `/api/interrupts/${APPROVAL}/decision`)).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/decision"))!.body).toEqual({
      decision: "approve",
      plan_version: 2,
      change_set_hash: (fx("approval_card.four_eyes").props as { change_set_hash: string }).change_set_hash,
    });
  });

  it("the approver who already approved sees that, not the buttons (after a reload)", async () => {
    renderCase([fx("approval_card.four_eyes")], { me: MARCO(), interrupt: APPROVAL, link: { status: "active", scopes: ["changes:apply"], apply_ready: true } });
    await waitFor(() => expect(document.querySelector("[data-decided]")).toHaveTextContent("Recorded for Marco Rossi · waiting for the other approvers"));
    expect(screen.queryByRole("group", { name: "Approval decision" })).toBeNull();
  });

  it("reject with a reason posts it", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase([fx("approval_card.four_eyes")], { me: LENA(), interrupt: APPROVAL });
    await user.click(await screen.findByRole("button", { name: "Reject" }));
    await user.type(screen.getByRole("textbox", { name: "Reason (optional)" }), "Vendor not approved");
    await user.click(screen.getByRole("button", { name: "Reject changes" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/decision"))?.body).toEqual({ decision: "reject", plan_version: 2, reason: "Vendor not approved" }));
  });

  it("guide checklist: Mark done posts the step (also while a run is active); progress and links render", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderCase([fx("remediation_checklist")]);
    const steps = await screen.findByRole("list", { name: "Steps" });
    expect(screen.getByRole("meter", { name: "Guide progress" })).toHaveAttribute("aria-valuetext", "2 of 5 steps done, 1 verified in Merxa");
    await user.click(within(within(steps).getAllByRole("listitem")[2]!).getByRole("button", { name: "Mark done" }));
    await waitFor(() => expect(calls.some((c) => c.url === `/api/cases/${CASE}/checklists/chk_01a1merxa/steps/3`)).toBe(true));
    expect(calls.find((c) => c.url.includes("/checklists/"))!.body).toEqual({ status: "done" });
    await expectNoA11yViolations(container);
  });

  it("commit tracker Undo → confirm → rollback case action", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase([fx("commit_tracker.undo")]);
    await user.click(await screen.findByRole("button", { name: "Undo changes" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Undo these changes in Merxa?" });
    await user.click(within(dialog).getByRole("button", { name: "Undo changes" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/actions"))?.body).toEqual({ type: "rollback", change_set_id: "cs_019a0c50" }));
    const post = calls.find((c) => c.url.endsWith("/actions"))!;
    expect(post.headers["Idempotency-Key"]).toMatch(/[0-9a-f-]{36}/);
  });

  it("Undo waits while an approval is open (a requested rollback sits there; no second rollback)", async () => {
    // Regression (M5 review): the tracker's Undo stayed enabled while the rollback awaited its
    // (always human) approval, so a second click queued another rollback of the same change set.
    renderCase([fx("commit_tracker.undo"), fx("approval_card.four_eyes")], {
      me: MARCO(),
      interrupt: APPROVAL,
      link: { status: "active", scopes: ["changes:apply"], apply_ready: true },
    });
    const undo = await screen.findByRole("button", { name: "Undo changes" });
    await waitFor(() => expect(undo).toHaveAttribute("aria-disabled", "true"));
    expect(screen.getByText("Undo is available again once the pending approval is decided.")).toBeInTheDocument();
  });

  it("a compliance-only viewer cannot undo (no write role)", async () => {
    renderCase([fx("commit_tracker.undo")], { me: LENA() });
    const undo = await screen.findByRole("button", { name: "Undo changes" });
    expect(undo).toHaveAttribute("aria-disabled", "true");
  });

  it("re-plan: banner in the turn, 'Updated' in the header, Compare opens the version switcher", async () => {
    const user = userEvent.setup();
    const v1 = { ...fx("plan_diff"), id: "blk_pd_v1", props: { ...(fx("plan_diff").props as object), plan_version: 1 } };
    const v2 = fx("plan_diff");
    const { container } = renderCase([v1, fx("narrative.replan"), v2]);
    expect(await screen.findByText("Updated · Plan v2")).toBeInTheDocument();
    expect(container.querySelector("[data-replan-banner]")).toHaveTextContent("plan updated (v2)");
    await user.click(screen.getByRole("button", { name: "Compare v1 and v2" }));
    const drawer = await screen.findByRole("dialog", { name: "Plan versions" });
    const group = within(drawer).getByRole("group", { name: "Plan versions" });
    expect(within(group).getByRole("button", { name: /Plan v2/ })).toHaveAttribute("aria-pressed", "true");
    // v1 is committed in the banner's version list, so v2's change list is a delta on top of it
    expect(within(drawer).getByText("On top of v1 (already applied): 9 added")).toBeInTheDocument();
    await user.click(within(group).getByRole("button", { name: /Plan v1/ }));
    expect(within(drawer).getByRole("table", { name: "Changes in Plan v1" })).toBeInTheDocument();
  });

  it("mixed option shows the split on the card", async () => {
    renderCase([fx("recommended_option.mixed")]);
    expect(await screen.findByRole("region", { name: "How the changes are made" })).toHaveTextContent("Guide me through 2");
  });

  it("viewing a case marks it seen for the case list", async () => {
    renderCase([fx("narrative")]);
    expect(window.localStorage.getItem(`keelson-seen:${CASE}`)).not.toBeNull();
  });
});

// ───────────────────────────── case list badge ─────────────────────────────

describe("case list 'Updated' badge", () => {
  it("shows for cases changed since the viewer last opened them", () => {
    markCaseSeen("c1", new Date("2026-10-07T09:00:00Z"));
    markCaseSeen("c2", new Date("2026-10-07T11:00:00Z"));
    render(
      <CaseList
        items={[
          { id: "c1", title: "Trim delay", mode: "actual", status: "committed", updated_at: "2026-10-07T10:00:00Z", last_message_preview: null },
          { id: "c2", title: "Port strike", mode: "actual", status: "open", updated_at: "2026-10-07T10:00:00Z", last_message_preview: null },
          { id: "c3", title: "Never opened", mode: "actual", status: "open", updated_at: "2026-10-07T10:00:00Z", last_message_preview: null },
        ]}
      />,
    );
    expect(within(screen.getByRole("link", { name: /Trim delay/ })).getByText("Updated", { selector: "[data-tone]" })).toBeInTheDocument();
    expect(within(screen.getByRole("link", { name: /Port strike/ })).queryByText("Updated", { selector: "[data-tone]" })).toBeNull();
    expect(within(screen.getByRole("link", { name: /Never opened/ })).queryByText("Updated", { selector: "[data-tone]" })).toBeNull();
  });
});

// ───────────────────────────── demo reset ─────────────────────────────

const PERSONAS = [{ username: "priya", name: "Priya Shah", role_label: "Planner", initials: "PS" }];
const RESET_RESULT = {
  ok: true,
  keep_consents: true,
  started_at: "2026-10-07T10:00:00Z",
  duration_ms: 12_400,
  steps: [
    { step: "merxa_scenario_reset", status: "ok", duration_ms: 3100, detail: null, counts: { orders: 12 } },
    { step: "keelson_rows", status: "ok", duration_ms: 420, detail: null, counts: { case: 3 } },
    { step: "merxa_consents", status: "skipped", duration_ms: 0, detail: "rehearsal mode keeps consents" },
  ],
};

function renderSwitcher(me: ReturnType<typeof makeMe>, result: unknown = RESET_RESULT, status = 200) {
  const afterReset = vi.fn();
  const { api, calls } = fakeApi([
    (url) => (url === "/api/demo/personas" ? jsonResponse(200, PERSONAS) : undefined),
    (url) => (url === "/api/demo/reset" ? jsonResponse(status, result) : undefined),
  ]);
  const Wrapper = withSession(me, api);
  render(
    <Wrapper>
      <PersonaSwitcher afterReset={afterReset} />
    </Wrapper>,
  );
  return { calls, afterReset };
}

describe("demo reset (persona menu, presenters only)", () => {
  it("a presenter resets with rehearsal mode and is sent home", async () => {
    const user = userEvent.setup();
    const { calls, afterReset } = renderSwitcher(makeMe({ roles: ["demo-presenter", "planner"] }));
    await user.click(screen.getByRole("button", { name: /demo persona/i }));
    await user.click(await screen.findByRole("menuitem", { name: /Reset demo/ }));
    const dialog = await screen.findByRole("alertdialog", { name: "Reset the demo?" });
    await user.click(within(dialog).getByRole("checkbox", { name: "Rehearsal: keep Merxa connections" }));
    await user.click(within(dialog).getByRole("button", { name: "Reset demo" }));
    await waitFor(() => expect(afterReset).toHaveBeenCalledOnce());
    const post = calls.find((c) => c.url === "/api/demo/reset")!;
    expect(post.method).toBe("POST");
    expect(post.body).toEqual({ keep_consents: true });
    expect(post.headers["X-CSRF-Token"]).toBe("csrf-test");
  });

  it("409 reset_in_progress keeps the dialog open with a message", async () => {
    const user = userEvent.setup();
    const { afterReset } = renderSwitcher(makeMe({ roles: ["platform-admin"] }), { code: "reset_in_progress", message: "busy" }, 409);
    await user.click(screen.getByRole("button", { name: /demo persona/i }));
    await user.click(await screen.findByRole("menuitem", { name: /Reset demo/ }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Reset demo" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Reset demo" })).not.toHaveAttribute("aria-busy"));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(afterReset).not.toHaveBeenCalled();
  });

  it("is not offered to other personas", async () => {
    const user = userEvent.setup();
    renderSwitcher(makeMe({ roles: ["planner", "approver"] }));
    await user.click(screen.getByRole("button", { name: /demo persona/i }));
    await screen.findByRole("menuitem", { name: /Priya Shah/ });
    expect(screen.queryByRole("menuitem", { name: /Reset demo/ })).toBeNull();
  });
});

// ───────────────────────────── admin: Policies ─────────────────────────────

const DEFAULT_RULES = [
  { id: "refuse-unconfirmed", description: "What-if cases and plans that rely on unconfirmed values are never committed", when: 'case.mode == "what_if" || case.unconfirmed > 0', require: "refuse", priority: 10 },
  {
    id: "four-eyes",
    description: "Large or structural changes need two different people",
    when: 'cs.incremental_cost_cents > 1000000 || cs.ops.exists(o, o in ["po.create", "po_line.cancel", "production_order.resequence"])',
    require: { roles: ["approver", "compliance"], count: 2, exclude_initiator: true },
    priority: 20,
  },
  { id: "human-untrusted", description: "Signals need an approver", when: 'case.origin != "chat" || case.tainted', require: { roles: ["approver"], count: 1, exclude_initiator: false }, priority: 30 },
  {
    id: "auto-small-transfer",
    description: "Small transfers started by a planner are applied automatically",
    when: 'cs.ops.all(o, o in ["allocation.set_qty", "transfer_order.create"]) && cs.lines <= 10 && cs.units <= 500 && initiator.roles.exists(r, r == "planner")',
    require: "auto",
    priority: 40,
  },
  { id: "default", description: "Changes need an approver", when: "true", require: { roles: ["approver"], count: 1, exclude_initiator: false }, priority: 50 },
];

function policyServer(overrides: { put?: () => Response } = {}) {
  const routes: Route[] = [
    (url, init) =>
      url === "/api/admin/policies" && (init.method ?? "GET") === "GET"
        ? jsonResponse(200, {
            spec_version: "v7",
            policy: { version: "p-2", rules: DEFAULT_RULES },
            source: "spec",
            editable: true,
            evaluator_available: true,
            fallback: { roles: ["approver"], count: 1, exclude_initiator: false },
          })
        : undefined,
    (url, init) =>
      url === "/api/admin/policies" && init.method === "PUT"
        ? (overrides.put?.() ?? jsonResponse(201, { spec_version: "v8", policy: { version: "p-3", rules: DEFAULT_RULES }, source: "spec", compiled: true, guarded: [], unchanged: false }))
        : undefined,
    (url) =>
      url === "/api/admin/policies/test"
        ? jsonResponse(200, {
            policy_version: "p-2",
            decision: { outcome: "auto", rule_id: "auto-small-transfer", requirement: null, reason: "Small transfers started by a planner are applied automatically", policy_version: "p-2" },
            approved_by: "policy:auto-small-transfer@p-2",
            matched_rule: DEFAULT_RULES[3],
            floors: { must_refuse: false, auto_allowed: true },
          })
        : undefined,
  ];
  return routes;
}

function renderPolicies(me: ReturnType<typeof makeMe>, routes: Route[] = policyServer()) {
  const { api, calls } = fakeApi(routes);
  const Wrapper = withSession(me, api);
  const utils = render(
    <Wrapper>
      <AdminShell>
        <PoliciesPage />
      </AdminShell>
    </Wrapper>,
  );
  return { ...utils, calls };
}

const SAM = () => makeMe({ username: "sam", name: "Sam Okafor", roles: ["tenant-admin", "planner"] });
const AVA = () => makeMe({ username: "ava", name: "Ava Lindqvist", roles: ["platform-admin"] });
const ruleItem = (id: string) => document.querySelector(`[data-rule-id="${id}"]`) as HTMLElement;

describe("admin Policies page", () => {
  it("lists the rules in evaluation order with priorities and outcomes; nav has Policies", async () => {
    const { container } = renderPolicies(SAM());
    const list = await screen.findByRole("list", { name: "Rules in evaluation order" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((li) => li.getAttribute("data-rule-id"))).toEqual(["refuse-unconfirmed", "four-eyes", "human-untrusted", "auto-small-transfer", "default"]);
    expect(items[1]).toHaveTextContent("Then: 2 different people: Approver and Compliance (not the person who started it)");
    expect(items[0]).toHaveTextContent("Refuse");
    expect(screen.getByText("Policy p-2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Policies" })).toHaveAttribute("aria-current", "page");
    await expectNoA11yViolations(container);
  });

  it("a tenant admin raising the auto caps is told a second admin must approve (M7 change request)", async () => {
    const user = userEvent.setup();
    renderPolicies(SAM());
    await screen.findByRole("list", { name: "Rules in evaluation order" });
    await user.click(screen.getByRole("button", { name: "Edit auto-small-transfer" }));
    const editor = ruleItem("auto-small-transfer");
    const cond = within(editor).getByRole("textbox", { name: "Condition (CEL)" });
    await user.clear(cond);
    await user.click(cond);
    await user.paste('cs.ops.all(o, o in ["allocation.set_qty", "transfer_order.create"]) && cs.lines <= 10 && cs.units <= 5000 && initiator.roles.exists(r, r == "planner")');
    const summary = document.querySelector("[data-guarded-summary]");
    expect(summary).toHaveTextContent("Requires a second admin");
    expect(summary).toHaveTextContent("auto-small-transfer");
    expect(screen.getByRole("button", { name: "Request approval" })).not.toHaveAttribute("aria-disabled", "true");
  });

  it("a tenant admin can tighten (lower caps) and save: PUT base_version + rules in order", async () => {
    const user = userEvent.setup();
    const { calls } = renderPolicies(SAM());
    await screen.findByRole("list", { name: "Rules in evaluation order" });
    await user.click(screen.getByRole("button", { name: "Edit auto-small-transfer" }));
    const cond = within(ruleItem("auto-small-transfer")).getByRole("textbox", { name: "Condition (CEL)" });
    await user.clear(cond);
    await user.click(cond);
    await user.paste('cs.ops.all(o, o in ["allocation.set_qty", "transfer_order.create"]) && cs.lines <= 10 && cs.units <= 200 && initiator.roles.exists(r, r == "planner")');
    expect(document.querySelector("[data-guarded]")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT")!;
    const body = put.body as { base_version: string; rules: { id: string; when: string }[] };
    expect(body.base_version).toBe("v7");
    expect(body.rules.map((r) => r.id)).toEqual(["refuse-unconfirmed", "four-eyes", "human-untrusted", "auto-small-transfer", "default"]);
    expect(body.rules[3]!.when).toContain("cs.units <= 200");
  });

  it("a platform admin may save a guarded change; moving rules swaps priorities", async () => {
    const user = userEvent.setup();
    const { calls } = renderPolicies(AVA());
    await screen.findByRole("list", { name: "Rules in evaluation order" });
    await user.click(screen.getByRole("button", { name: "Move auto-small-transfer up" }));
    const ids = within(screen.getByRole("list", { name: "Rules in evaluation order" }))
      .getAllByRole("listitem")
      .map((li) => li.getAttribute("data-rule-id"));
    expect(ids).toEqual(["refuse-unconfirmed", "four-eyes", "auto-small-transfer", "human-untrusted", "default"]);
    expect(document.querySelector("[data-guarded-summary]")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const rules = (calls.find((c) => c.method === "PUT")!.body as { rules: { id: string; priority: number }[] }).rules;
    expect(rules.find((r) => r.id === "auto-small-transfer")!.priority).toBe(30);
    expect(rules.find((r) => r.id === "human-untrusted")!.priority).toBe(40);
  });

  it("CEL hints appear while typing; a rule with problems blocks Save", async () => {
    const user = userEvent.setup();
    renderPolicies(AVA());
    await screen.findByRole("list", { name: "Rules in evaluation order" });
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    const editor = ruleItem("rule-6");
    const cond = within(editor).getByRole("textbox", { name: "Condition (CEL)" });
    await user.clear(cond);
    await user.type(cond, "cs.qty = 5");
    const hints = editor.querySelector("[data-cel-hints]")!;
    expect(hints).toHaveTextContent("Use == to compare");
    expect(hints).toHaveTextContent("Unknown field cs.qty");
    // No description yet → validation blocks Save.
    expect(screen.getByRole("button", { name: "Save rules" })).toHaveAttribute("aria-disabled", "true");
  });

  it("422 policy_invalid lists the server's errors", async () => {
    const user = userEvent.setup();
    renderPolicies(
      AVA(),
      policyServer({
        put: () => jsonResponse(422, { code: "policy_invalid", message: "some policy conditions are not valid CEL over the policy context", errors: [{ loc: "rules.default.when", msg: "undeclared reference to 'foo'" }] }),
      }),
    );
    await screen.findByRole("list", { name: "Rules in evaluation order" });
    await user.click(screen.getByRole("button", { name: "Move default up" }));
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    expect(await screen.findByText("rules.default.when: undeclared reference to 'foo'")).toBeInTheDocument();
  });

  it("test bench: POST /api/admin/policies/test with the sample context; the matched rule is highlighted", async () => {
    const user = userEvent.setup();
    const { calls } = renderPolicies(SAM());
    await screen.findByRole("list", { name: "Rules in evaluation order" });
    await user.click(screen.getByRole("button", { name: "Test rules" }));
    await waitFor(() => expect(document.querySelector("[data-test-result]")).toHaveTextContent("Applied automatically"));
    const post = calls.find((c) => c.url === "/api/admin/policies/test")!;
    expect(post.body).toEqual({
      context: {
        cs: { lines: 4, units: 320, value_cents: 0, ops: ["transfer_order.create"], connectors: ["merxa"], incremental_cost_cents: 120000, lost_units_after: 0, levers: ["reallocate_inventory"] },
        case: { mode: "actual", origin: "chat", tainted: false, unconfirmed: 0 },
        initiator: { roles: ["planner"] },
      },
    });
    expect(screen.getByText("policy:auto-small-transfer@p-2")).toBeInTheDocument();
    expect(ruleItem("auto-small-transfer")).toHaveAttribute("data-matched");
  });

  it("test bench refuses an invalid sample without calling the server; drafts are sent with the rules", async () => {
    const user = userEvent.setup();
    const { calls } = renderPolicies(AVA());
    await screen.findByRole("list", { name: "Rules in evaluation order" });
    const lines = screen.getByRole("spinbutton", { name: "Lines" });
    await user.clear(lines);
    await user.click(screen.getByRole("button", { name: "Test rules" }));
    expect(await screen.findByText(/The sample is not valid: cs\.lines/)).toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/admin/policies/test")).toBe(false);
    await user.type(lines, "3");
    await user.click(screen.getByRole("button", { name: "Move default up" }));
    await user.click(screen.getByRole("button", { name: "Test draft rules" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/policies/test")).toBe(true));
    expect((calls.find((c) => c.url === "/api/admin/policies/test")!.body as { rules?: unknown[] }).rules).toHaveLength(5);
  });
});
