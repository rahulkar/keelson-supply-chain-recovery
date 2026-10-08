import { toast } from "@keelson/ui";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import type { CaseStreamAction } from "../stream/reducer";
import { InvalidDecisionError, decisionBody, rollbackBody, useCaseActions, type CaseActionDeps } from "./useCaseActions";

vi.mock("@keelson/ui", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }) }));

function setup(api: Partial<ApiClient>) {
  const dispatched: CaseStreamAction[] = [];
  const refresh = vi.fn();
  const deps: CaseActionDeps = { api: api as ApiClient, caseId: "case-1", dispatch: (a) => dispatched.push(a), refresh, navigate: vi.fn() };
  const { result } = renderHook(() => useCaseActions(deps));
  return { result, dispatched, refresh };
}

beforeEach(() => vi.clearAllMocks());

describe("decision reasons (M5)", () => {
  it("reject / revise carry the trimmed reason; other decisions never do", () => {
    expect(decisionBody("reject", { plan_version: 2, reason: "  Vendor not approved  " })).toEqual({ decision: "reject", plan_version: 2, reason: "Vendor not approved" });
    expect(decisionBody("revise", { plan_version: 2, reason: "Smaller batch" })).toEqual({ decision: "revise", plan_version: 2, reason: "Smaller batch" });
    expect(decisionBody("approve", { plan_version: 2, change_set_hash: "a".repeat(64), reason: "fine" })).toEqual({
      decision: "approve",
      plan_version: 2,
      change_set_hash: "a".repeat(64),
    });
    expect(decisionBody("reject", { plan_version: 2, reason: "   " })).toEqual({ decision: "reject", plan_version: 2 });
  });

  it("an over-long reason is refused, never cut short", () => {
    expect(() => decisionBody("reject", { reason: "x".repeat(501) })).toThrow(InvalidDecisionError);
    expect(decisionBody("reject", { reason: "x".repeat(500) }).reason).toHaveLength(500);
  });
});

describe("rollback (Undo changes)", () => {
  it("rollbackBody only carries the change set id", () => {
    expect(rollbackBody(" cs_1 ")).toEqual({ type: "rollback", change_set_id: "cs_1" });
    expect(() => rollbackBody("")).toThrow(InvalidDecisionError);
    expect(() => rollbackBody("x".repeat(201))).toThrow(InvalidDecisionError);
  });

  it("posts the case action with an Idempotency-Key reused until the server answers", async () => {
    const postAction = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(0, "network_error", "offline"))
      .mockResolvedValueOnce("run-rb");
    const { result, dispatched } = setup({ postAction });
    await act(() => result.current.onBlockAction({ type: "rollback", changeSetId: "cs_019a0c50" }));
    await act(() => result.current.onBlockAction({ type: "rollback", changeSetId: "cs_019a0c50" }));
    expect(postAction).toHaveBeenCalledTimes(2);
    expect(postAction.mock.calls[0]![1]).toEqual({ type: "rollback", change_set_id: "cs_019a0c50" });
    expect(postAction.mock.calls[0]![2]).toBe(postAction.mock.calls[1]![2]);
    expect(dispatched).toEqual([{ type: "expect_run", runId: "run-rb" }]);
    expect(toast.error).toHaveBeenCalledWith("Couldn't undo the changes", expect.anything());
  });

  it("409 not_undoable refreshes the case", async () => {
    const postAction = vi.fn().mockRejectedValue(new ApiError(409, "not_undoable", "only applied changes can be undone"));
    const { result, refresh } = setup({ postAction });
    await act(() => result.current.onBlockAction({ type: "rollback", changeSetId: "cs_1" }));
    expect(refresh).toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith("Couldn't undo the changes", { description: "Only applied changes can be undone." });
  });
});

describe("guide checklist ticks", () => {
  it("posts the manual status to the step endpoint (no run is started)", async () => {
    const tickChecklistStep = vi.fn(async () => ({ step: { seq: 3, status: "done" }, changed: true }));
    const { result, dispatched } = setup({ tickChecklistStep });
    await act(() => result.current.onBlockAction({ type: "checklist_step", checklistId: "chk_1", seq: 3, status: "done" }));
    expect(tickChecklistStep).toHaveBeenCalledWith("case-1", "chk_1", 3, "done");
    expect(dispatched).toEqual([]);
  });

  it("already verified → an informational toast, not an error", async () => {
    const tickChecklistStep = vi.fn().mockRejectedValue(new ApiError(409, "already_verified", "already verified"));
    const { result } = setup({ tickChecklistStep });
    await act(() => result.current.onBlockAction({ type: "checklist_step", checklistId: "chk_1", seq: 1, status: "skipped" }));
    expect(toast).toHaveBeenCalledWith("This step is already verified", expect.anything());
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("other failures say so", async () => {
    const tickChecklistStep = vi.fn().mockRejectedValue(new ApiError(404, "not_found", "step not found"));
    const { result } = setup({ tickChecklistStep });
    await act(() => result.current.onBlockAction({ type: "checklist_step", checklistId: "chk_1", seq: 9, status: "done" }));
    expect(toast.error).toHaveBeenCalledWith("Couldn't update the step", { description: "Step not found." });
  });
});

describe("ApiClient M5 routes", () => {
  const fake = (body: unknown, status = 200) => {
    const calls: { url: string; init: RequestInit }[] = [];
    const fetch = vi.fn(async (url: RequestInfo | URL, init: RequestInit = {}) => {
      calls.push({ url: String(url), init });
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    });
    return { api: new ApiClient({ fetch: fetch as unknown as typeof globalThis.fetch, csrfToken: "t", onUnauthorized: () => {} }), calls };
  };

  it("tickChecklistStep encodes ids and sends {status} with CSRF", async () => {
    const { api, calls } = fake({ step: { seq: 2, status: "skipped" }, changed: true });
    await api.tickChecklistStep("c/1", "chk 1", 2, "skipped");
    expect(calls[0]!.url).toBe("/api/cases/c%2F1/checklists/chk%201/steps/2");
    expect(calls[0]!.init.method).toBe("POST");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ status: "skipped" });
    expect((calls[0]!.init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("t");
    await expect(api.tickChecklistStep("c", "k", 0, "done")).rejects.toBeInstanceOf(ApiError);
    // never a hand-set "verified" (only the engine's read-back verifies a step)
    await expect(api.tickChecklistStep("c", "k", 1, "verified" as "done")).rejects.toBeInstanceOf(ApiError);
    expect(calls).toHaveLength(1);
  });

  it("demoReset posts keep_consents and parses the step log", async () => {
    const { api, calls } = fake({
      ok: true,
      keep_consents: true,
      started_at: "2026-10-07T10:00:00Z",
      duration_ms: 12_400,
      steps: [{ step: "merxa_scenario_reset", status: "ok", duration_ms: 3100, detail: null, counts: { orders: 12 } }],
    });
    const out = await api.demoReset(true);
    expect(calls[0]!.url).toBe("/api/demo/reset");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ keep_consents: true });
    expect(out.steps[0]).toMatchObject({ step: "merxa_scenario_reset", status: "ok" });
  });
});
