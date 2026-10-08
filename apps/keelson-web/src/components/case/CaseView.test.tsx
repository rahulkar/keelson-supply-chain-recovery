import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { FakeEventSource } from "../../../test/fakeEventSource";
import { M1_FLOW, WHAT_IF, runId, ssePayload, type RecordedEvent, type Recording } from "../../../test/fixtures/recorded";
import type { CaseDetail } from "@/lib/api/types";
import { caseDetail, fakeApi, jsonResponse, makeMe, withSession } from "../../../test/session";
import { setMatchMedia } from "../../../test/setup";
import { CaseView } from "./CaseView";

const CASE = M1_FLOW.case_id;
const CHAT = runId(M1_FLOW, "chat");
const USER_TEXT = "Our trim supplier says wadding and zips are delayed 7 days - what's the impact?";

const APPROACH_ID = `approach-${CASE}-1`;
const APPROVAL_ID = "approval-cs-1-1";

function until(rec: Recording, seq: number): RecordedEvent[] {
  return rec.events.slice(0, rec.events.findIndex((e) => e.seq === seq) + 1);
}

/**
 * The fake engine's case state: `GET /api/cases/{id}` answers with the events emitted so far
 * (`last_event_seq`) and the interrupts PENDING at that point (`open_interrupts`), like the
 * real snapshot. `pending` can be overridden to script other server states.
 */
const server: { lastSeq: number; pending: (seq: number) => string[]; link: Record<string, unknown> | null } = {
  lastSeq: 0,
  pending: m1Pending,
  link: null,
};

/** `GET /api/connectors/merxa/link` for the viewer (`null`: 500). */
const LINKED = { status: "active", ext_username: "marco", roles: ["approver"], scopes: ["openid", "changes:apply"], apply_ready: true };
const UNLINKED = { status: "missing", ext_username: null, roles: [], scopes: [], apply_ready: false };

function m1Pending(seq: number): string[] {
  if (seq >= 17 && seq < 18) return [APPROACH_ID];
  if (seq >= 66 && seq < 67) return [APPROVAL_ID];
  return [];
}

function openInterrupts(seq: number) {
  return server.pending(seq).map((id) => ({
    id,
    kind: id.startsWith("approval") ? "approval" : "approach",
    message: null,
    payload: {},
    response_schema: {},
  }));
}

function emit(es: FakeEventSource, events: RecordedEvent[]) {
  for (const e of events) {
    if (e.persist) server.lastSeq = Math.max(server.lastSeq, e.seq);
    es.emit(ssePayload(e), e.persist ? String(e.seq) : "");
  }
}

function stream(events: RecordedEvent[]) {
  const es = FakeEventSource.latest();
  act(() => {
    es.open();
    emit(es, events);
  });
}

function renderCase(
  opts: {
    me?: ReturnType<typeof makeMe>;
    lastSeq?: number;
    rec?: Recording;
    extraMessages?: CaseDetail["messages"];
    decided?: CaseDetail["decided_interrupts"];
    /** M6: a case opened from an alert (no user message; its signal run is announced as pending). */
    pendingRuns?: CaseDetail["pending_runs"];
    noUserMessage?: boolean;
  } = {},
) {
  const rec = opts.rec ?? M1_FLOW;
  const snapshot = () => ({
    ...detail,
    last_event_seq: server.lastSeq,
    open_interrupts: openInterrupts(server.lastSeq),
    ...(opts.decided ? { decided_interrupts: opts.decided } : {}),
  });
  const { api, calls } = fakeApi([
    (url) => (url === `/api/cases/${rec.case_id}` ? jsonResponse(200, snapshot()) : undefined),
    (url) => (url.endsWith("/decision") ? jsonResponse(202, { status: "resumed", run_id: "run-next" }) : undefined),
    (url) => (url.endsWith("/messages") ? jsonResponse(202, { run_id: "run-msg" }) : undefined),
    (url) => (url.endsWith("/actions") ? jsonResponse(202, { run_id: "run-act" }) : undefined),
    (url) => (url.includes("/cancel") ? jsonResponse(202, { status: "running", cancel_requested: true }) : undefined),
    (url) =>
      url === "/api/connectors/merxa/link"
        ? server.link
          ? jsonResponse(200, server.link)
          : jsonResponse(500, { code: "internal_error", message: "x", error_id: "e" })
        : undefined,
  ]);
  server.lastSeq = opts.lastSeq ?? 0;
  const detail = caseDetail(rec.case_id, {
    messages: [
      ...(opts.noUserMessage
        ? []
        : [{ id: "1", role: "user", text: USER_TEXT, run_id: rec.runs[0]![1], created_at: "2026-10-05T09:00:00Z" }]),
      ...(opts.extraMessages ?? []),
    ],
    last_event_seq: server.lastSeq,
    open_interrupts: rec === M1_FLOW ? openInterrupts(server.lastSeq) : [],
    ...(opts.decided ? { decided_interrupts: opts.decided } : {}),
    ...(opts.pendingRuns ? { pending_runs: opts.pendingRuns } : {}),
  });
  const Wrapper = withSession(opts.me ?? makeMe(), api);
  const utils = render(
    <Wrapper>
      <CaseView initial={detail} streamOptions={{ createEventSource: FakeEventSource.factory }} />
    </Wrapper>,
  );
  return { ...utils, calls };
}

beforeEach(() => {
  FakeEventSource.reset();
  window.localStorage.clear();
  server.lastSeq = 0;
  server.pending = m1Pending;
  server.link = LINKED;
  window.history.replaceState({}, "", "/");
});

const MARCO = () => makeMe({ username: "marco", name: "Marco Rossi", roles: ["planner", "approver"] });
const linkCalls = (calls: { url: string }[]) => calls.filter((c) => c.url === "/api/connectors/merxa/link").length;

describe("CaseView", () => {
  it("renders the full thread from a replay: user bubble, timelines, narrative once, inline blocks, follow-ups", async () => {
    renderCase({ lastSeq: 80 });
    expect(FakeEventSource.latest().url).toBe(`/api/cases/${CASE}/events?after=0`);
    stream(M1_FLOW.events.filter((e) => e.persist));

    expect(screen.getByRole("heading", { level: 1, name: "Trim delay" })).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "You said" })).toHaveTextContent(USER_TEXT);
    // one thinking timeline per run
    expect(screen.getAllByRole("button", { name: /show thinking/i })).toHaveLength(4);
    // the narrative appears once (TEXT_MESSAGE), not again as a narrative block
    expect(screen.getAllByText(/Rebalance stock from DE1 to NL1 to protect the peak weeks/)).toHaveLength(1);
    expect(screen.getByText("Rebalance between DCs")).toBeInTheDocument();
    expect(screen.getByText(/verified them by reading them back/)).toBeInTheDocument();
    // the approach and approval interrupts are closed: no dead buttons, a note instead (70 §6)
    expect(screen.queryByRole("group", { name: "Approach decision" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Approval decision" })).not.toBeInTheDocument();
    expect(screen.getAllByText("No longer waiting for a decision").length).toBeGreaterThanOrEqual(2);
    // follow-ups only at the end of the last finished turn
    const followUps = screen.getByRole("list", { name: "Follow-up questions" });
    expect(within(followUps).getByRole("button", { name: "Summarise what changed in Merxa" })).toBeInTheDocument();
    expect(screen.getAllByRole("list", { name: "Follow-up questions" })).toHaveLength(1);
  });

  it("confirms the approach through POST /api/interrupts/{id}/decision", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase({ lastSeq: 17 });
    stream(until(M1_FLOW, 17));
    const approach = screen.getByRole("group", { name: "Approach decision" });
    const confirm = within(approach).getByRole("button", { name: "Looks right" });
    expect(confirm).not.toHaveAttribute("aria-disabled", "true");
    await user.click(confirm);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/decision"))).toBe(true));
    const call = calls.find((c) => c.url.endsWith("/decision"))!;
    expect(call.url).toBe(`/api/interrupts/approach-${CASE}-1/decision`);
    expect(call.method).toBe("POST");
    expect(call.body).toEqual({ decision: "confirm" });
    expect(call.headers["X-CSRF-Token"]).toBe("csrf-test");
    // the card now says who decided instead of keeping dead buttons
    await waitFor(() => expect(screen.queryByRole("group", { name: "Approach decision" })).not.toBeInTheDocument());
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
    expect(screen.getByText(/^Decided by Priya Shah · \d{1,2} \w{3} \d{4}, \d{2}:\d{2} UTC$/)).toBeInTheDocument();
  });

  it("shows who decided from the snapshot's decided_interrupts on a replayed case (70 §6 carry-over)", () => {
    renderCase({
      lastSeq: 80,
      decided: [
        { id: APPROACH_ID, decision: "confirm", decided_by: [{ name: "Priya Shah", username: "priya" }], decided_at: "2026-10-05T09:02:00Z" },
        { id: APPROVAL_ID, decision: "approve", decided_by: [{ name: "Marco Rossi" }], resolved_at: "2026-10-05T09:15:00Z" },
      ],
    });
    stream(M1_FLOW.events.filter((e) => e.persist));
    expect(screen.getByText("Decided by Priya Shah · 5 Oct 2026, 09:02 UTC")).toBeInTheDocument();
    expect(screen.getByText("Decided by Marco Rossi · 5 Oct 2026, 09:15 UTC")).toBeInTheDocument();
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("does not let a planner without the approver role approve", () => {
    renderCase({ lastSeq: 66 });
    stream(until(M1_FLOW, 66));
    const decision = screen.getByRole("group", { name: "Approval decision" });
    expect(within(decision).getByRole("button", { name: "Approve" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Waiting for someone with the approver role to decide.")).toBeInTheDocument();
    expect(screen.getByText("Awaiting approval")).toBeInTheDocument();
  });

  it("lets a linked approver approve with the plan version and change-set hash", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase({ lastSeq: 66, me: MARCO() });
    stream(until(M1_FLOW, 66));
    const decision = screen.getByRole("group", { name: "Approval decision" });
    // Approve waits for the viewer's link check
    expect(within(decision).getByRole("button", { name: "Approve" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Checking your Merxa connection…")).toBeInTheDocument();
    await waitFor(() => expect(within(decision).getByRole("button", { name: "Approve" })).not.toHaveAttribute("aria-disabled", "true"));
    expect(linkCalls(calls)).toBe(1);
    await user.click(within(decision).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/decision"))).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/decision"))!.body).toEqual({
      decision: "approve",
      plan_version: 1,
      change_set_hash: "cdd9ffb9c14e1a8a63084a9c2f099b5d41a0d727079e181a7fb1600a89c4dd85",
    });
  });

  it("asks an unlinked approver to Connect Merxa although the card says the initiator is linked (regression)", async () => {
    const user = userEvent.setup();
    server.link = UNLINKED;
    window.history.replaceState({}, "", `/cases/${CASE}`);
    const { calls } = renderCase({ lastSeq: 66, me: MARCO() });
    stream(until(M1_FLOW, 66));
    // the recorded approval_card carries connect.status "active" (evaluated for the initiator)
    const connect = await screen.findByRole("button", { name: "Connect Merxa" });
    const approve = within(screen.getByRole("group", { name: "Approval decision" })).getByRole("button", { name: "Approve" });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(approve).toHaveAccessibleDescription(/Connect your Merxa account/);
    await user.click(approve);
    expect(calls.some((c) => c.url.endsWith("/decision"))).toBe(false);
    expect(connect).toBeInTheDocument();
  });

  it("offers Approve when the link check fails (the decision API's 412 still decides)", async () => {
    server.link = null;
    renderCase({ lastSeq: 66, me: MARCO() });
    stream(until(M1_FLOW, 66));
    const decision = screen.getByRole("group", { name: "Approval decision" });
    await waitFor(() => expect(within(decision).getByRole("button", { name: "Approve" })).not.toHaveAttribute("aria-disabled", "true"));
    expect(screen.queryByRole("button", { name: /Connect Merxa/ })).not.toBeInTheDocument();
  });

  it("checks the viewer's link only while an approval card they can act on is shown", async () => {
    const planner = renderCase({ lastSeq: 66 });
    stream(until(M1_FLOW, 66));
    await act(async () => {});
    expect(linkCalls(planner.calls)).toBe(0); // no approver role
    expect(screen.queryByRole("button", { name: /Connect Merxa/ })).not.toBeInTheDocument();
    planner.unmount();
    FakeEventSource.reset();
    const done = renderCase({ lastSeq: 80, me: MARCO() });
    stream(M1_FLOW.events.filter((e) => e.persist));
    await act(async () => {});
    expect(linkCalls(done.calls)).toBe(0); // the approval was decided
  });

  it("re-checks the link when the tab becomes visible again", async () => {
    server.link = UNLINKED;
    const { calls } = renderCase({ lastSeq: 66, me: MARCO() });
    stream(until(M1_FLOW, 66));
    await screen.findByRole("button", { name: "Connect Merxa" });
    server.link = LINKED;
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Connect Merxa" })).not.toBeInTheDocument());
    expect(linkCalls(calls)).toBe(2);
  });

  // Browser e2e finding (1280 px): the canvas slide-in pushed the page 12 px wider for a moment.
  it("clips the case layout so the canvas reveal never scrolls the page sideways", () => {
    const { getByTestId } = renderCase();
    expect(getByTestId("case-layout")).toHaveClass("overflow-hidden");
  });

  it("drops ?connected=merxa from the URL after returning from the connect flow", async () => {
    window.history.replaceState({}, "", `/cases/${CASE}?connected=merxa&x=1`);
    renderCase({ lastSeq: 66, me: MARCO() });
    await act(async () => {});
    expect(window.location.pathname + window.location.search).toBe(`/cases/${CASE}?x=1`);
  });

  it("starts 'Apply changes for me' as a case action with an Idempotency-Key", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase({ lastSeq: 57 });
    stream(until(M1_FLOW, 57));
    const recommended = screen.getByText("Rebalance between DCs").closest("section")!;
    const apply = within(recommended as HTMLElement).getAllByRole("button").find((b) => /apply/i.test(b.textContent ?? ""))!;
    await user.click(apply);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/actions"))).toBe(true));
    const call = calls.find((c) => c.url.endsWith("/actions"))!;
    expect(call.body).toEqual({ type: "apply", option_id: "opt-A", plan_version: 1 });
    expect(call.headers["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("holds the plan's action buttons while its changes await approval, and after they were applied", () => {
    const { unmount } = renderCase({ lastSeq: 66 });
    stream(until(M1_FLOW, 66));
    const apply = () => screen.getAllByRole("button").find((b) => /apply changes/i.test(b.textContent ?? ""))!;
    expect(apply()).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("The changes for this plan are waiting for approval.")).toBeInTheDocument();
    unmount();
    FakeEventSource.reset();
    renderCase({ lastSeq: 80 });
    stream(M1_FLOW.events.filter((e) => e.persist));
    expect(apply()).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("These changes were applied in Merxa.")).toBeInTheDocument();
  });

  it("sends a follow-up from the composer and shows it as a queued turn", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase({ lastSeq: 57 });
    stream(until(M1_FLOW, 57));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "What about NL1?{Enter}");
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/messages"))).toBe(true));
    const call = calls.find((c) => c.url.endsWith("/messages"))!;
    expect(call.body).toEqual({ text: "What about NL1?" });
    expect(call.headers["Idempotency-Key"]).toBeTruthy();
    await waitFor(() => expect(screen.getAllByRole("article", { name: "You said" })).toHaveLength(2));
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
    expect(screen.getByText("Getting started…")).toBeInTheDocument();
  });

  it("offers Stop while a run is in progress and cancels it", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase({ lastSeq: 25 });
    stream(until(M1_FLOW, 25));
    const composer = screen.getByRole("textbox", { name: "Message" }).closest("form")!;
    await user.click(within(composer).getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/cancel"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/cancel"))!.url).toBe(`/api/runs/${runId(M1_FLOW, "resume_approach")}/cancel`);
  });

  it("auto-opens the canvas on the first live heavy result (wide screens) and lists the Trace", async () => {
    setMatchMedia((q) => q.includes("min-width"));
    const user = userEvent.setup();
    renderCase({ lastSeq: 17 });
    stream(until(M1_FLOW, 17));
    expect(screen.queryByRole("complementary", { name: "Canvas" })).not.toBeInTheDocument();
    act(() => emit(FakeEventSource.latest(), M1_FLOW.events.filter((x) => x.seq > 17 && x.seq <= 57)));
    const canvas = await screen.findByRole("complementary", { name: "Canvas" });
    expect(within(canvas).getByRole("tab", { name: /Result/ })).toHaveAttribute("aria-selected", "true");
    expect(within(canvas).getByText("Changes in Merxa")).toBeInTheDocument();
    await user.click(within(canvas).getByRole("tab", { name: /Trace/ }));
    expect(within(canvas).getByText("get_inventory")).toBeInTheDocument();
    await user.click(within(canvas).getByRole("tab", { name: /Evidence/ }));
    expect(within(canvas).getAllByText(/list locations/).length).toBeGreaterThan(0);
  });

  it("does not auto-open the canvas for results that were only replayed", () => {
    setMatchMedia((q) => q.includes("min-width"));
    renderCase({ lastSeq: 80 });
    stream(M1_FLOW.events.filter((e) => e.persist));
    expect(screen.queryByRole("complementary", { name: "Canvas" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open canvas" })).toBeInTheDocument();
  });

  it("is read-only for compliance", () => {
    renderCase({ lastSeq: 17, me: makeMe({ username: "lena", name: "Lena Vogel", roles: ["compliance"] }) });
    stream(until(M1_FLOW, 17));
    expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
    expect(screen.getByText("Your role can follow this case but not change it.")).toBeInTheDocument();
    const approach = screen.getByRole("group", { name: "Approach decision" });
    expect(within(approach).getByRole("button", { name: "Looks right" })).toHaveAttribute("aria-disabled", "true");
  });

  it("shows a what-if case's badge and the refusal to apply", () => {
    renderCase({ rec: WHAT_IF, lastSeq: 61 });
    stream(WHAT_IF.events.filter((e) => e.persist));
    expect(screen.getAllByText("What-if").length).toBeGreaterThan(0);
    const refused = document.querySelector<HTMLElement>(`[data-run-id="${runId(WHAT_IF, "action_refused")}"]`)!;
    expect(within(refused).getByText(/promote it to an actual case before applying changes/)).toBeInTheDocument();
  });

  it("keeps the approval decidable while a follow-up question runs (regression)", async () => {
    const marco = makeMe({ username: "marco", name: "Marco Rossi", roles: ["planner", "approver"] });
    const { calls } = renderCase({ lastSeq: 66, me: marco });
    stream(until(M1_FLOW, 66));
    // Priya asks a question in another tab: a chat run starts; the approval stays PENDING.
    server.pending = (seq) => (seq >= 66 ? [APPROVAL_ID] : m1Pending(seq));
    const q = "019a0c4f-0000-7000-8000-0000000000aa";
    const sc = { tenantId: "demo-retail", caseId: CASE, stage: "understanding", prov: "fact", label: "Run started" };
    const before = calls.filter((c) => c.url === `/api/cases/${CASE}`).length;
    act(() => {
      server.lastSeq = 67;
      FakeEventSource.latest().emit(
        JSON.stringify({ type: "RUN_STARTED", threadId: CASE, runId: q, metadata: { sc: { ...sc, seq: 67 } } }),
        "67",
      );
    });
    // the snapshot is re-read on the live RUN_STARTED (and its answer keeps the card open)
    await waitFor(() => expect(calls.filter((c) => c.url === `/api/cases/${CASE}`).length).toBeGreaterThan(before));
    await act(async () => {});
    const decision = screen.getByRole("group", { name: "Approval decision" });
    expect(within(decision).getByRole("button", { name: "Approve" })).not.toHaveAttribute("aria-disabled", "true");
  });

  it("closes an interrupt decided elsewhere once the refreshed snapshot no longer lists it", async () => {
    renderCase({ lastSeq: 17 });
    stream(until(M1_FLOW, 17));
    const approach = screen.getByRole("group", { name: "Approach decision" });
    expect(within(approach).getByRole("button", { name: "Looks right" })).not.toHaveAttribute("aria-disabled", "true");
    // someone else confirmed: the resume run starts
    act(() => emit(FakeEventSource.latest(), M1_FLOW.events.filter((x) => x.seq === 18)));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Approach decision" })).not.toBeInTheDocument());
    expect(screen.getByText("No longer waiting for a decision")).toBeInTheDocument();
  });

  it("offers Stop on a queued turn only after the replay and while nothing else runs", () => {
    renderCase({ lastSeq: 80 });
    // before the replay every past run still looks queued: no Stop anywhere
    expect(screen.queryByRole("button", { name: /^Stop/ })).not.toBeInTheDocument();
    stream(M1_FLOW.events.filter((e) => e.persist));
    expect(screen.queryByRole("button", { name: /^Stop/ })).not.toBeInTheDocument();
  });

  it("stops a queued turn while another run is running, without touching the running one (sc.runId)", async () => {
    const user = userEvent.setup();
    const { calls } = renderCase({
      lastSeq: 25,
      extraMessages: [{ id: "2", role: "user", text: "And NL1?", run_id: "run-q", created_at: "2030-01-01T00:00:00Z" }],
    });
    stream(until(M1_FLOW, 25));
    const queued = () => document.querySelector<HTMLElement>('[data-run-id="run-q"]')!;
    const running = () => document.querySelector<HTMLElement>(`[data-run-id="${runId(M1_FLOW, "resume_approach")}"]`)!;
    expect(within(queued()).getByText("Getting started…")).toBeInTheDocument();
    expect(within(running()).getByRole("button", { name: /stop/i })).toBeInTheDocument();
    await user.click(within(queued()).getByRole("button", { name: /stop/i }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/runs/run-q/cancel")).toBe(true));
    const sc = { tenantId: "demo-retail", caseId: CASE, stage: "understanding", prov: "decision", label: "Stopped", runId: "run-q" };
    act(() => {
      server.lastSeq = 26;
      FakeEventSource.latest().emit(
        JSON.stringify({ type: "RUN_ERROR", message: "Run cancelled.", code: "CANCELLED", metadata: { sc: { ...sc, seq: 26 } } }),
        "26",
      );
    });
    expect(within(queued()).getByText("Stopped")).toBeInTheDocument();
    expect(within(running()).getByRole("button", { name: /stop/i })).toBeInTheDocument();
    expect(within(running()).queryByText("Stopped")).not.toBeInTheDocument();
    await act(async () => {});
  });

  it("shows a failed run's generic message and error ID, never exception text", async () => {
    const errorId = "6f1e7c8a-9d55-4b1c-8d3e-4f500b1c2d3e";
    renderCase({ lastSeq: 25 });
    stream(until(M1_FLOW, 25));
    const sc = { tenantId: "demo-retail", caseId: CASE, stage: "checking_systems", prov: "fact", label: "Failed", runId: runId(M1_FLOW, "resume_approach") };
    act(() => {
      server.lastSeq = 26;
      FakeEventSource.latest().emit(
        JSON.stringify({
          type: "RUN_ERROR",
          message: `Something went wrong while working on this case. Please try again; if it keeps happening, contact support with error ID ${errorId}.`,
          code: "INTERNAL_ERROR",
          metadata: { sc: { ...sc, seq: 26 }, errorId },
        }),
        "26",
      );
      FakeEventSource.latest().emit(
        JSON.stringify({
          type: "RUN_ERROR",
          message: 'Traceback (most recent call last):\n  File "/srv/app.py", line 1\nValueError: secret=abc',
          code: "ValueError",
          metadata: { sc: { ...sc, runId: CHAT, seq: 27 } },
        }),
        "27",
      );
    });
    const failed = within(document.querySelector<HTMLElement>(`[data-run-id="${runId(M1_FLOW, "resume_approach")}"]`)!);
    expect(failed.getByTestId("run-error")).toHaveTextContent(`contact support with error ID ${errorId}`);
    expect(failed.getByRole("button", { name: "Copy error ID" })).toBeInTheDocument();
    expect(failed.getByText(/Stopped before finishing/)).toBeInTheDocument();
    const screened = within(document.querySelector<HTMLElement>(`[data-run-id="${CHAT}"]`)!);
    expect(screened.getByTestId("run-error")).toHaveTextContent("Something went wrong while working on this case.");
    expect(document.body).not.toHaveTextContent(/Traceback|secret=abc|ValueError/);
    await act(async () => {});
  });

  it("returns focus to the canvas toggle when the canvas is closed from inside", async () => {
    setMatchMedia((q) => q.includes("min-width"));
    const user = userEvent.setup();
    renderCase({ lastSeq: 80 });
    stream(M1_FLOW.events.filter((e) => e.persist));
    const toggle = screen.getByRole("button", { name: "Open canvas" });
    await user.click(toggle);
    const canvas = await screen.findByRole("complementary", { name: "Canvas" });
    await user.click(within(canvas).getByRole("tab", { name: /Trace/ }));
    await user.click(within(canvas).getByRole("button", { name: "Close canvas" }));
    expect(screen.getByRole("button", { name: "Open canvas" })).toHaveFocus();
  });

  it("shows a posted message as queued until its run starts", async () => {
    renderCase({ lastSeq: 0 });
    expect(screen.getByRole("article", { name: "You said" })).toHaveTextContent(USER_TEXT);
    expect(screen.getByText("Getting started…")).toBeInTheDocument();
    stream(until(M1_FLOW, 3));
    expect(document.querySelector(`[data-run-id="${CHAT}"]`)).not.toBeNull();
    expect(screen.getByText("Understanding…")).toBeInTheDocument();
    await act(async () => {}); // the snapshot refresh triggered by the live RUN_STARTED
  });

  it("shows the queued signal run of a case opened from an alert (no user message) as a waiting turn", async () => {
    renderCase({ lastSeq: 0, noUserMessage: true, pendingRuns: [{ id: "run-signal", kind: "signal" }] });
    expect(screen.queryByRole("article", { name: "You said" })).toBeNull();
    expect(document.querySelector('[data-run-id="run-signal"]')).not.toBeNull();
    expect(screen.getByText("Getting started…")).toBeInTheDocument();
    await act(async () => {});
  });

  it("posts a message once even when it is submitted twice before re-rendering", async () => {
    const { calls } = renderCase({ lastSeq: 57 });
    stream(until(M1_FLOW, 57));
    const box = screen.getByRole("textbox", { name: "Message" });
    act(() => {
      box.focus();
    });
    const form = box.closest("form")!;
    await act(async () => {
      fireEvent.change(box, { target: { value: "Twice?" } });
    });
    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/messages"))).toHaveLength(1));
  });
});
