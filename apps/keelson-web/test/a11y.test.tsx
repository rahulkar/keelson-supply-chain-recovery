import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CaseView } from "@/components/case/CaseView";
import { HomeComposer } from "@/components/home/HomeComposer";
import { AppShell } from "@/components/shell/AppShell";
import { expectNoA11yViolations } from "./a11y";
import { FakeEventSource } from "./fakeEventSource";
import { M1_FLOW, ssePayload } from "./fixtures/recorded";
import { caseDetail, fakeApi, jsonResponse, makeMe, withSession } from "./session";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/" }));

// Full case threads + axe: ~2.6 s alone, past the 5 s default when Docker builds share the CPU.
vi.setConfig({ testTimeout: 20_000 });

beforeEach(() => FakeEventSource.reset());

describe("accessibility (axe-core)", () => {
  it("app shell + home composer", async () => {
    const { container } = render(
      <AppShell me={makeMe()}>
        <h1>Hello, Priya</h1>
        <HomeComposer />
      </AppShell>,
    );
    await act(async () => {}); // let next/link settle its prefetch state
    await expectNoA11yViolations(container);
  });

  it("case thread with every M1 block, paused for approval", async () => {
    const { api } = fakeApi([]);
    const Wrapper = withSession(makeMe({ roles: ["planner", "approver"] }), api);
    const detail = caseDetail(M1_FLOW.case_id, {
      messages: [{ id: "1", role: "user", text: "Trims are 7 days late", run_id: M1_FLOW.runs[0]![1], created_at: null }],
      last_event_seq: 66,
      open_interrupts: [{ id: "approval-cs-1-1", kind: "approval", message: null, payload: {}, response_schema: {} }],
    });
    const { container } = render(
      <Wrapper>
        <h1>Keelson</h1>
        <CaseView initial={detail} streamOptions={{ createEventSource: FakeEventSource.factory }} />
      </Wrapper>,
    );
    act(() => {
      const es = FakeEventSource.latest();
      es.open();
      for (const e of M1_FLOW.events.filter((x) => x.persist && x.seq <= 66)) es.emit(ssePayload(e), String(e.seq));
    });
    await act(async () => {}); // the viewer link check (approver) settles
    await expectNoA11yViolations(container);
  });

  it("case thread with several turns, a failed run and the Connect Merxa call to action", async () => {
    const { api } = fakeApi([
      (url) => (url === "/api/connectors/merxa/link" ? jsonResponse(200, { status: "missing", scopes: [], roles: [], apply_ready: false }) : undefined),
    ]);
    const Wrapper = withSession(makeMe({ roles: ["planner", "approver"] }), api);
    const detail = caseDetail(M1_FLOW.case_id, {
      messages: [{ id: "1", role: "user", text: "Trims are 7 days late", run_id: M1_FLOW.runs[0]![1], created_at: null }],
      last_event_seq: 66,
      open_interrupts: [{ id: "approval-cs-1-1", kind: "approval", message: null, payload: {}, response_schema: {} }],
    });
    const { container, findByRole, getAllByRole } = render(
      <Wrapper>
        <h1>Keelson</h1>
        <CaseView initial={detail} streamOptions={{ createEventSource: FakeEventSource.factory }} />
      </Wrapper>,
    );
    act(() => {
      const es = FakeEventSource.latest();
      es.open();
      for (const e of M1_FLOW.events.filter((x) => x.persist && x.seq <= 66)) es.emit(ssePayload(e), String(e.seq));
      const sc = { tenantId: "demo-retail", caseId: M1_FLOW.case_id, stage: "understanding", prov: "fact", label: "Failed", runId: "run-failed", seq: 67 };
      es.emit(
        JSON.stringify({ type: "RUN_ERROR", message: "Something went wrong while working on this case.", code: "INTERNAL_ERROR", metadata: { sc, errorId: "6f1e7c8a-9d55-4b1c-8d3e-4f500b1c2d3e" } }),
        "67",
      );
    });
    await findByRole("button", { name: "Connect Merxa" });
    const names = getAllByRole("region").map((r) => r.getAttribute("aria-label")).filter((n) => n?.startsWith("Thinking"));
    expect(names.length).toBeGreaterThan(1);
    expect(new Set(names).size).toBe(names.length);
    await expectNoA11yViolations(container);
  });
});
