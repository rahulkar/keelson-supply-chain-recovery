import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import type { CaseStreamAction } from "../stream/reducer";
import { InvalidDecisionError, actionBody, decisionBody, typedEdits, useCaseActions, type CaseActionDeps } from "./useCaseActions";

vi.mock("@keelson/ui", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }) }));

function setup(api: Partial<ApiClient>) {
  const dispatched: CaseStreamAction[] = [];
  const navigate = vi.fn();
  const refresh = vi.fn();
  const openCanvas = vi.fn();
  const deps: CaseActionDeps = {
    api: api as ApiClient,
    caseId: "case-1",
    dispatch: (a) => dispatched.push(a),
    refresh,
    navigate,
    openCanvas,
  };
  const { result } = renderHook(() => useCaseActions(deps));
  return { result, dispatched, navigate, refresh, openCanvas };
}

describe("useCaseActions", () => {
  it("posts a decision with only the contract fields and announces the resumed run", async () => {
    const decide = vi.fn(async () => ({ status: "resumed" as const, run_id: "run-9" }));
    const { result, dispatched } = setup({ decide });
    await act(() =>
      result.current.onBlockAction({
        type: "interrupt_decision",
        interruptId: "approval-cs-1-1",
        decision: "approve",
        payload: { plan_version: 2, change_set_hash: "f".repeat(64), extra: "dropped" },
      }),
    );
    expect(decide).toHaveBeenCalledWith("approval-cs-1-1", { decision: "approve", plan_version: 2, change_set_hash: "f".repeat(64) });
    expect(dispatched).toEqual([
      { type: "interrupt_decided", interruptId: "approval-cs-1-1", decision: "approve", at: expect.any(String), recorded: false },
      { type: "expect_run", runId: "run-9" },
    ]);
  });

  it("sends approach edits", async () => {
    const decide = vi.fn(async () => ({ status: "resumed" as const, run_id: "r" }));
    const { result } = setup({ decide });
    await act(() =>
      result.current.onBlockAction({ type: "interrupt_decision", interruptId: "approach-1", decision: "edit", payload: { edits: "  It is 14 days  " } }),
    );
    expect(decide).toHaveBeenCalledWith("approach-1", { decision: "edit", edits: "It is 14 days" });
  });

  it("412 connect_required → navigates to the validated connect URL, back to this case", async () => {
    window.history.replaceState(null, "", "/cases/case-1");
    const decide = vi.fn(async () => {
      throw new ApiError(412, "connect_required", "connect", {
        connect_url: `${window.location.origin}/api/connectors/merxa/connect?return_to=%2Fcases%2Fcase-1`,
      });
    });
    const { result, navigate, dispatched } = setup({ decide });
    await act(() => result.current.onBlockAction({ type: "interrupt_decision", interruptId: "a", decision: "approve" }));
    expect(navigate).toHaveBeenCalledWith("/api/connectors/merxa/connect?return_to=%2Fcases%2Fcase-1");
    expect(dispatched).toEqual([]);
  });

  it("a foreign connect_url is replaced by our own", async () => {
    window.history.replaceState(null, "", "/cases/case-1");
    const decide = vi.fn(async () => {
      throw new ApiError(412, "connect_required", "connect", { connect_url: "https://evil.example/steal" });
    });
    const { result, navigate } = setup({ decide });
    await act(() => result.current.onBlockAction({ type: "interrupt_decision", interruptId: "a", decision: "approve" }));
    expect(navigate).toHaveBeenCalledWith("/api/connectors/merxa/connect?return_to=%2Fcases%2Fcase-1");
  });

  it("409 refreshes the case", async () => {
    const decide = vi.fn(async () => {
      throw new ApiError(409, "stale", "changed");
    });
    const { result, refresh } = setup({ decide });
    await act(() => result.current.onBlockAction({ type: "interrupt_decision", interruptId: "a", decision: "approve" }));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("reuses the Idempotency-Key for a retried case action until the server answers", async () => {
    const postAction = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(0, "network_error", "offline"))
      .mockResolvedValueOnce("run-1")
      .mockResolvedValueOnce("run-2");
    const { result, dispatched } = setup({ postAction });
    const action = { type: "case_action" as const, action: "apply" as const, optionId: "opt-A", planVersion: 1 };
    await act(() => result.current.onBlockAction(action));
    await act(() => result.current.onBlockAction(action));
    await act(() => result.current.onBlockAction(action));
    const keys = postAction.mock.calls.map((c) => c[2] as string);
    expect(postAction.mock.calls[0]![1]).toEqual({ type: "apply", option_id: "opt-A", plan_version: 1 });
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[1]);
    expect(dispatched).toEqual([
      { type: "expect_run", runId: "run-1" },
      { type: "expect_run", runId: "run-2" },
    ]);
  });

  it("reuses the message key after a network failure but not after a 4xx", async () => {
    const postMessage = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(0, "network_error", "offline"))
      .mockRejectedValueOnce(new ApiError(422, "empty_message", "empty"))
      .mockResolvedValueOnce("run-3");
    const { result } = setup({ postMessage });
    await act(async () => {
      expect(await result.current.sendMessage("hello")).toBe(false);
    });
    await act(async () => {
      expect(await result.current.sendMessage("hello")).toBe(false);
    });
    await act(async () => {
      expect(await result.current.sendMessage("hello")).toBe(true);
    });
    const keys = postMessage.mock.calls.map((c) => c[2] as string);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[1]);
  });

  it("connect and open_canvas never call the API", async () => {
    const { result, navigate, openCanvas } = setup({});
    await act(() => result.current.onBlockAction({ type: "connect", connector: "merxa", returnTo: "/cases/case-1" }));
    expect(navigate).toHaveBeenCalledWith("/api/connectors/merxa/connect?return_to=%2Fcases%2Fcase-1");
    await act(() => result.current.onBlockAction({ type: "open_canvas", blockId: "blk_1" }));
    expect(openCanvas).toHaveBeenCalledWith("blk_1");
  });

  it("cancels a run without touching the stream state (the RUN_ERROR names the run)", async () => {
    const cancelRun = vi.fn(async () => {});
    const { result, dispatched } = setup({ cancelRun });
    await act(() => result.current.cancelRun("run-1"));
    expect(cancelRun).toHaveBeenCalledWith("run-1");
    expect(dispatched).toEqual([]);
  });

  it("reports a refused cancel request", async () => {
    const { toast } = await import("@keelson/ui");
    const cancelRun = vi.fn(async () => {
      throw new ApiError(404, "not_found", "run not found");
    });
    const { result, dispatched } = setup({ cancelRun });
    await act(() => result.current.cancelRun("run-1"));
    expect(dispatched).toEqual([]);
    expect(toast.error).toHaveBeenCalledWith("Couldn't stop the run", { description: "Run not found." });
  });
});

describe("M4 decisions and actions", () => {
  it("sends typed approach edits and removed checks (docs/contracts/90 §2)", async () => {
    const decide = vi.fn(async () => ({ status: "resumed" as const, run_id: "r" }));
    const { result } = setup({ decide });
    await act(() =>
      result.current.onBlockAction({
        type: "interrupt_decision",
        interruptId: "approach-c-1",
        decision: "edit",
        payload: {
          edits: [
            { key: "duration_days", value_int: 14 },
            { key: "affected_receipts", value_ids: ["PO-1#1", "PO-2#1"] },
          ],
          removed_checks: ["t5", "t5"],
        },
      }),
    );
    expect(decide).toHaveBeenCalledWith("approach-c-1", {
      decision: "edit",
      edits: [
        { key: "duration_days", value_int: 14 },
        { key: "affected_receipts", value_ids: ["PO-1#1", "PO-2#1"] },
      ],
      removed_checks: ["t5"],
    });
  });

  it("a malformed typed edit list is never sent half-way", () => {
    expect(typedEdits([{ key: "Bad Key", value_int: 1 }])).toBeNull();
    expect(typedEdits([{ key: "d", value_int: 1.5 }])).toBeNull();
    expect(typedEdits([{ key: "d", value_int: 1, value_ids: ["x"] }])).toBeNull();
    expect(typedEdits([{ key: "ids", value_ids: [] }])).toBeNull();
    expect(typedEdits([{ key: "ids", value_ids: ["a", "a"] }])).toBeNull();
    expect(typedEdits([{ key: "ids", value_ids: Array.from({ length: 501 }, (_, i) => `x${i}`) }])).toBeNull();
    expect(typedEdits(Array.from({ length: 51 }, () => ({ key: "d", value_int: 1 })))).toBeNull();
    expect(typedEdits([{ key: "d", value_int: 3, value_ids: null }])).toEqual([{ key: "d", value_int: 3 }]);
    // Regression: dropping only the bad part would send a bare `edit` (the approach redone
    // without the user's changes); the whole decision is refused instead.
    expect(() => decisionBody("edit", { edits: [{ key: "Bad", value_int: 1 }], removed_checks: ["t1"] })).toThrow(InvalidDecisionError);
    expect(() => decisionBody("edit", { removed_checks: Array.from({ length: 51 }, (_, i) => `t${i}`) })).toThrow(InvalidDecisionError);
    expect(() => decisionBody("edit", { removed_checks: "t1" })).toThrow(InvalidDecisionError);
    expect(decisionBody("edit", { edits: [], removed_checks: ["t1"] })).toEqual({ decision: "edit", edits: [], removed_checks: ["t1"] });
    expect(decisionBody("confirm", { removed_checks: null })).toEqual({ decision: "confirm" });
  });

  it("a malformed typed edit is not posted; the user is told why", async () => {
    const { toast } = await import("@keelson/ui");
    const decide = vi.fn(async () => ({ status: "resumed" as const, run_id: "r" }));
    const { result, dispatched } = setup({ decide });
    await act(() =>
      result.current.onBlockAction({
        type: "interrupt_decision",
        interruptId: "approach-1",
        decision: "edit",
        payload: { edits: [{ key: "delay_days", value_int: 1.5 }] },
      }),
    );
    expect(decide).not.toHaveBeenCalled();
    expect(dispatched).toEqual([]);
    expect(toast.error).toHaveBeenCalledWith("That didn't go through", { description: "Some of the changed values are not valid." });
  });

  it("confirm_assumptions never goes out with an empty key list (the engine reads [] as 'all pending')", async () => {
    const { toast } = await import("@keelson/ui");
    const postAction = vi.fn(async () => "run-c");
    const { result, dispatched } = setup({ postAction });
    for (const assumptionKeys of [[], undefined, Array.from({ length: 51 }, (_, i) => `k${i}`)]) {
      await act(() =>
        result.current.onBlockAction({ type: "case_action", action: "confirm_assumptions", optionId: "opt-B", planVersion: 2, assumptionKeys }),
      );
    }
    expect(postAction).not.toHaveBeenCalled();
    expect(dispatched).toEqual([]);
    expect(toast.error).toHaveBeenCalledWith("That didn't go through", { description: "There are no values to confirm, or too many at once." });
    expect(() => actionBody({ type: "case_action", action: "confirm_assumptions", optionId: "o", planVersion: 1, assumptionKeys: [" ", ""] })).toThrow(
      InvalidDecisionError,
    );
  });

  it("confirm_assumptions carries the keys; promote is a plain action", async () => {
    const postAction = vi.fn(async () => "run-c");
    const { result, dispatched } = setup({ postAction });
    await act(() =>
      result.current.onBlockAction({
        type: "case_action",
        action: "confirm_assumptions",
        optionId: "opt-B",
        planVersion: 2,
        assumptionKeys: ["delay_days"],
      }),
    );
    expect(postAction).toHaveBeenCalledWith(
      "case-1",
      { type: "confirm_assumptions", option_id: "opt-B", plan_version: 2, assumption_keys: ["delay_days"] },
      expect.any(String),
    );
    expect(dispatched).toEqual([{ type: "expect_run", runId: "run-c" }]);
    expect(actionBody({ type: "case_action", action: "promote", optionId: "opt-B", planVersion: 2 })).toEqual({
      type: "promote",
      option_id: "opt-B",
      plan_version: 2,
    });
  });
});
