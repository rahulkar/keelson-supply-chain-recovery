import { remediationChecklistPropsSchema, type BlockEnvelope, type PlanDiffRow, type RemediationChecklistProps } from "@keelson/contracts";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  BlockRenderer,
  DONE_STEPS,
  COMMIT_STATUS_TEXT,
  PlanVersionSwitcher,
  ReplanNotice,
  approvalNeedsLink,
  checklistAnnouncement,
  checklistProgress,
  comparePlanDiffs,
  comparisonSentence,
  isDeltaOf,
  mixSentence,
  optionActions,
  quorumSummary,
  UNCONFIRMED_APPLY_REASON,
  WHAT_IF_APPLY_REASON,
  safeDeepLink,
  undoAction,
  viewerApproval,
  type BlockAction,
  type BlockViewer,
} from "../src";
import { blockFixtures, getFixture } from "../src/fixtures";
import { expectNoA11yViolations } from "./a11y";

const fixture = (name: string): BlockEnvelope => {
  const f = getFixture(name);
  if (!f) throw new Error(`missing fixture ${name}`);
  return structuredClone(f.envelope);
};

const renderBlock = (envelope: BlockEnvelope, extra: { busy?: boolean; viewer?: BlockViewer } = {}) => {
  const onAction = vi.fn<(a: BlockAction) => Promise<void>>().mockResolvedValue(undefined);
  const utils = render(<BlockRenderer envelope={envelope} onAction={onAction} {...extra} />);
  return { ...utils, onAction };
};

type QuorumProps = {
  quorum: {
    required: { roles: string[]; count: number; exclude_initiator: boolean };
    approvals: { sub: string; name: string; role: string; at: string }[];
    remaining: number;
    missing_roles: string[];
  } | null;
};

const ready: BlockViewer["connections"] = { merxa: { status: "active", applyReady: true } };
const missing: BlockViewer["connections"] = { merxa: { status: "missing", applyReady: false } };

describe("M5 fixtures", () => {
  it("are appended after the M4 ones (e2e keeps its order)", () => {
    const names = blockFixtures.map((f) => f.name);
    expect(names.indexOf("approval_card.four_eyes")).toBeGreaterThan(names.indexOf("narrative.what_changed"));
    const start = names.indexOf("approval_card.four_eyes");
    expect(names.slice(start, start + 10)).toEqual([
      "approval_card.four_eyes",
      "approval_card.rejected",
      "approval_card.bundle",
      "recommended_option.mixed",
      "remediation_checklist",
      "remediation_checklist.completed",
      "commit_tracker.undo",
      "commit_tracker.conflicts",
      "commit_tracker.rolled_back",
      "narrative.replan",
    ]);
  });
});

describe("approval quorum (four-eyes)", () => {
  it("shows progress: '1 of 2 approvals: Marco Rossi ✓ · waiting for Compliance'", async () => {
    const { container } = renderBlock(fixture("approval_card.four_eyes"), { viewer: { connections: ready, sub: "user:lena", roles: ["compliance"] } });
    const region = screen.getByRole("region", { name: "Approvals" });
    expect(within(region).getByText("1 of 2 approvals: Marco Rossi ✓ · waiting for Compliance")).toBeInTheDocument();
    expect(within(region).getByRole("meter", { name: "Approvals" })).toHaveAttribute("aria-valuetext", "1 of 2 approvals");
    expect(within(region).getByText("The person who started this change can't approve it.")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("summarises any-of quorums and met quorums", () => {
    const base = (fixture("approval_card.four_eyes").props as QuorumProps).quorum!;
    expect(quorumSummary({ ...base, approvals: [], remaining: 2, missing_roles: ["approver", "compliance"] }).text).toBe(
      "0 of 2 approvals: waiting for Approver and Compliance",
    );
    expect(
      quorumSummary({
        required: { roles: ["approver"], count: 2, exclude_initiator: false },
        approvals: [base.approvals[0]!],
        remaining: 1,
        missing_roles: [],
      }).text,
    ).toBe("1 of 2 approvals: Marco Rossi ✓ · waiting for 1 more approval");
    const met = quorumSummary({
      ...base,
      approvals: [...base.approvals, { sub: "user:lena", name: "Lena Vogel", role: "compliance", at: "2026-10-07T09:20:00Z" }],
      remaining: 0,
      missing_roles: [],
    });
    expect(met).toMatchObject({ approved: 2, needed: 2, met: true, waiting: null });
    expect(met.text).toBe("2 of 2 approvals: Marco Rossi ✓ · Lena Vogel ✓");
  });

  it("a compliance co-approver can approve without a Merxa link (only the executing approver needs one)", async () => {
    const user = userEvent.setup();
    const env = fixture("approval_card.four_eyes");
    const { onAction, container } = renderBlock(env, { viewer: { connections: missing, sub: "user:lena", roles: ["compliance"] } });
    expect(container.querySelector("[data-connect-state]")).toBeNull();
    expect(container.querySelector("[data-co-approval]")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAction).toHaveBeenCalledWith({
      type: "interrupt_decision",
      interruptId: "approval-cs_01a1f4eyes",
      decision: "approve",
      payload: { plan_version: 2, change_set_hash: (env.props as { change_set_hash: string }).change_set_hash },
    });
  });

  it("an approver who would execute still needs an active link", () => {
    const env = fixture("approval_card.four_eyes");
    (env.props as QuorumProps).quorum!.approvals = [];
    (env.props as QuorumProps).quorum!.remaining = 2;
    (env.props as QuorumProps).quorum!.missing_roles = ["approver", "compliance"];
    const { container } = renderBlock(env, { viewer: { connections: missing, sub: "user:marco", roles: ["approver"] } });
    expect(container.querySelector('[data-connect-state="connect"]')).not.toBeNull();
    expect(screen.getByRole("button", { name: "Approve" })).toHaveAttribute("aria-disabled", "true");
  });

  it("approvalNeedsLink: no quorum / unknown roles keep the M2 rule", () => {
    const props = fixture("approval_card.four_eyes").props as QuorumProps;
    expect(approvalNeedsLink({ quorum: null }, { roles: ["compliance"] })).toBe(true);
    expect(approvalNeedsLink(props as never, undefined)).toBe(true);
    expect(approvalNeedsLink(props as never, { roles: ["compliance"] })).toBe(false);
    // The approver slot is taken (Marco): another approver would not execute.
    expect(approvalNeedsLink(props as never, { roles: ["approver", "compliance"] })).toBe(false);
    // A quorum that does not name `approver`: the deciding member executes.
    expect(
      approvalNeedsLink({ quorum: { ...props.quorum!, required: { roles: ["ops-director"], count: 1, exclude_initiator: false } } } as never, {
        roles: ["ops-director"],
      }),
    ).toBe(true);
  });

  it("a viewer who already approved sees 'recorded … waiting for the other approvers' instead of buttons", () => {
    const env = fixture("approval_card.four_eyes");
    expect(viewerApproval((env.props as QuorumProps).quorum as never, { sub: "user:marco" })?.name).toBe("Marco Rossi");
    const { container } = renderBlock(env, { viewer: { connections: ready, sub: "user:marco", roles: ["approver"] } });
    expect(screen.queryByRole("group", { name: "Approval decision" })).toBeNull();
    expect(container.querySelector("[data-decided]")).toHaveTextContent("Recorded for Marco Rossi · waiting for the other approvers");
  });

  it("reject opens a reason form and sends the trimmed reason", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("approval_card.four_eyes"), { viewer: { connections: ready, sub: "user:lena", roles: ["compliance"] } });
    await user.click(screen.getByRole("button", { name: "Reject" }));
    const form = screen.getByRole("form", { name: "Reject these changes" });
    await user.type(within(form).getByRole("textbox", { name: "Reason (optional)" }), "  Vendor not approved yet  ");
    await user.click(within(form).getByRole("button", { name: "Reject changes" }));
    expect(onAction).toHaveBeenCalledWith({
      type: "interrupt_decision",
      interruptId: "approval-cs_01a1f4eyes",
      decision: "reject",
      payload: { plan_version: 2, reason: "Vendor not approved yet" },
    });
  });

  it("an empty reason is not sent; Cancel returns to the buttons", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("approval_card.connected"));
    await user.click(screen.getByRole("button", { name: "Revise" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("group", { name: "Approval decision" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Revise" }));
    await user.click(screen.getByRole("button", { name: "Send back" }));
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ decision: "revise", payload: { plan_version: 2 } }));
  });

  it("a rejected card shows who rejected and why, without buttons", async () => {
    const { container } = renderBlock(fixture("approval_card.rejected"));
    const note = container.querySelector("[data-rejection]")!;
    expect(note).toHaveTextContent("Rejected by Lena Vogel (Compliance)");
    expect(note).toHaveTextContent("Reason: The new supplier PO-BD-0031 is not on the approved vendor list yet.");
    expect(screen.queryByRole("group", { name: "Approval decision" })).toBeNull();
    expect(container.querySelector("[data-quorum]")).toBeNull();
    await expectNoA11yViolations(container);
  });

  it("a bundle lists every system the approval covers", () => {
    renderBlock(fixture("approval_card.bundle"));
    const systems = screen.getByRole("region", { name: "Systems changed" });
    expect(systems).toHaveTextContent("One approval covers the changes in 2 systems:");
    expect(within(systems).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Merxa9 linescs_01a1bndl_merxa",
      "Warehouse (WMS)2 linescs_01a1bndl_wms",
    ]);
  });
});

describe("recommended option: mixed apply + guide", () => {
  it("mixSentence", () => {
    expect(mixSentence(null)).toBeNull();
    expect(mixSentence({ apply_lines: 5, guide_lines: 0 })).toBeNull();
    expect(mixSentence({ apply_lines: 3, guide_lines: 2 })).toBe("Apply 3 changes for me · Guide me through 2");
    expect(mixSentence({ apply_lines: 1, guide_lines: 1 })).toBe("Apply 1 change for me · Guide me through 1");
    expect(mixSentence({ apply_lines: 0, guide_lines: 4 })).toBe("Guide me through all 4 changes");
  });

  it("optionActions never offers Guide on a what-if or with unconfirmed values either", () => {
    // Regression (M5 review): only Apply was made safe; Guide also acts on the plan (95 §1 rule 1).
    const guide = { type: "guide" as const, label: "Guide me", enabled: true, disabled_reason: null };
    expect(optionActions({ actions: [guide], what_if: true, unconfirmed: [] })[0]).toEqual({ ...guide, enabled: false, disabled_reason: WHAT_IF_APPLY_REASON });
    expect(optionActions({ actions: [guide], what_if: false, unconfirmed: [{ key: "k", label: "Delay", source: "news", value_text: "7 days" }] })[0]).toEqual({
      ...guide,
      enabled: false,
      disabled_reason: UNCONFIRMED_APPLY_REASON,
    });
    expect(optionActions({ actions: [guide], what_if: false, unconfirmed: [] })).toEqual([guide]);
  });

  it("shows the split and describes Apply with it", async () => {
    const user = userEvent.setup();
    const { container, onAction } = renderBlock(fixture("recommended_option.mixed"));
    const mix = screen.getByRole("region", { name: "How the changes are made" });
    expect(mix).toHaveTextContent("Apply 3 changes for me");
    expect(mix).toHaveTextContent("Guide me through 2");
    expect(mix).toHaveTextContent("Apply makes 3 changes for you and gives you a step-by-step checklist for the other 2.");
    const apply = screen.getByRole("button", { name: "Apply changes" });
    expect(apply).toHaveAccessibleDescription("Apply 3 changes for me · Guide me through 2");
    await user.click(apply);
    expect(onAction).toHaveBeenCalledWith({ type: "case_action", action: "apply", optionId: "opt_rebalance", planVersion: 2 });
    await expectNoA11yViolations(container);
  });
});

describe("remediation checklist (Guide mode)", () => {
  const props = () => fixture("remediation_checklist").props as RemediationChecklistProps;

  it("renders steps, statuses, progress and deep links that open Merxa in a new tab", async () => {
    const { container } = renderBlock(fixture("remediation_checklist"));
    const steps = within(screen.getByRole("list", { name: "Steps" })).getAllByRole("listitem");
    expect(steps).toHaveLength(5);
    expect(steps.map((s) => s.getAttribute("data-step-status"))).toEqual(["verified", "done", "pending", "pending", "pending"]);
    expect(steps[0]).toHaveTextContent("Verified");
    expect(steps[1]).toHaveTextContent("Done · waiting for the check");
    expect(screen.getByRole("meter", { name: "Guide progress" })).toHaveAttribute("aria-valuetext", "2 of 5 steps done, 1 verified in Merxa");
    const links = screen.getAllByRole("link", { name: /Open in Merxa/ });
    // Verified steps no longer offer their link; step 5 has none.
    expect(links).toHaveLength(3);
    for (const a of links) {
      expect(a).toHaveAttribute("target", "_blank");
      expect(a).toHaveAttribute("rel", "noopener noreferrer");
      expect(a.getAttribute("href")).toMatch(/^http:\/\/merxa\.localhost\//);
    }
    expect(container.querySelector("[data-guide-completed]")).toBeNull();
    await expectNoA11yViolations(container);
  });

  it("manual done / skip raise checklist_step actions", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("remediation_checklist"));
    const steps = within(screen.getByRole("list", { name: "Steps" })).getAllByRole("listitem");
    // A done step can still be skipped, but not marked done again.
    expect(within(steps[1]!).queryByRole("button", { name: "Mark done" })).toBeNull();
    await user.click(within(steps[2]!).getByRole("button", { name: "Mark done" }));
    expect(onAction).toHaveBeenLastCalledWith({ type: "checklist_step", checklistId: "chk_01a1merxa", seq: 3, status: "done" });
    await user.click(within(steps[4]!).getByRole("button", { name: "Skip" }));
    expect(onAction).toHaveBeenLastCalledWith({ type: "checklist_step", checklistId: "chk_01a1merxa", seq: 5, status: "skipped" });
    // Verified steps offer nothing.
    expect(within(steps[0]!).queryByRole("button")).toBeNull();
  });

  it("busy disables the manual ticks", () => {
    renderBlock(fixture("remediation_checklist"), { busy: true });
    for (const b of screen.getAllByRole("button", { name: /Mark done|Skip/ })) expect(b).toHaveAttribute("aria-disabled", "true");
  });

  it("live ticks from the stream are announced, and completion shows 'Guide completed'", async () => {
    const onAction = vi.fn();
    const env = fixture("remediation_checklist");
    const { rerender, container } = render(<BlockRenderer envelope={env} onAction={onAction} />);
    const next = structuredClone(env);
    const p = next.props as RemediationChecklistProps;
    p.steps[1]!.status = "verified";
    p.steps[1]!.verified_at = "2026-10-07T10:05:00Z";
    rerender(<BlockRenderer envelope={next} onAction={onAction} />);
    await waitFor(() => expect(screen.getByText("Step 2 verified in Merxa")).toBeInTheDocument());
    const done = fixture("remediation_checklist.completed");
    rerender(<BlockRenderer envelope={{ ...done, id: env.id }} onAction={onAction} />);
    await waitFor(() => expect(container.querySelector("[data-guide-completed]")).toHaveTextContent("Guide completed"));
    expect(screen.getAllByText("Guide completed").length).toBeGreaterThan(0);
  });

  it("checklistProgress / checklistAnnouncement", () => {
    expect(checklistProgress(props())).toEqual({ done: 2, total: 5 });
    expect(checklistProgress({ ...props(), progress: { done: 4, total: 5 } })).toEqual({ done: 4, total: 5 });
    const prev = props().steps;
    const next = prev.map((s) => (s.seq === 3 ? { ...s, status: "skipped" as const } : s.seq === 2 ? { ...s, status: "verified" as const } : s));
    expect(checklistAnnouncement(prev, next, "merxa")).toBe("Step 2 verified in Merxa. Step 3 skipped");
    expect(checklistAnnouncement(prev, prev, "merxa")).toBe("");
  });

  it("deep links: the schema refuses unsafe schemes, the block only links allowlisted pages", () => {
    for (const bad of ["javascript:alert(1)", "data:text/html,x", "//evil.example/x", "http://user@merxa.localhost/x", "/\\evil"]) {
      const p = props();
      p.steps[2]!.deep_link = bad;
      expect(remediationChecklistPropsSchema.safeParse(p).success, bad).toBe(false);
    }
    expect(safeDeepLink("http://merxa.localhost/transfer-orders/new?from=DE1")).toBe("http://merxa.localhost/transfer-orders/new?from=DE1");
    expect(safeDeepLink("https://evil.example/phish")).toBeNull();
    expect(safeDeepLink("http://merxa.localhost/v1/admin/scenario:reset")).toBeNull();
    expect(safeDeepLink("/cases/abc")).toBe("/cases/abc");
    expect(safeDeepLink("/api/demo/reset")).toBeNull();
    expect(safeDeepLink("//evil.example")).toBeNull();
    expect(safeDeepLink(null)).toBeNull();
  });

  it("safeDeepLink judges the resolved path: dot segments can't smuggle a link into the API", () => {
    // Regression (M5 review): "/./api/…" and "/x/../api/…" passed a raw prefix check, and the
    // browser resolves them to /api/… (e.g. the GET that starts the Merxa OAuth hop).
    expect(safeDeepLink("/./api/connectors/merxa/connect?return_to=/")).toBeNull();
    expect(safeDeepLink("/cases/../api/demo/reset")).toBeNull();
    expect(safeDeepLink("/%2e/api/x")).toBeNull();
    expect(safeDeepLink("/%2e%2e/API/x")).toBeNull();
    expect(safeDeepLink("/api?x=1")).toBeNull();
    expect(safeDeepLink("/apiary/x")).toBe("/apiary/x");
    expect(safeDeepLink("/transfer-orders/new?from=DE1&to=NL1")).toBe("/transfer-orders/new?from=DE1&to=NL1");
  });

  it("a deep link to a host outside the allowlist renders no link (the step stays usable)", () => {
    const env = fixture("remediation_checklist");
    (env.props as RemediationChecklistProps).steps[2]!.deep_link = "https://elsewhere.example/po/2";
    renderBlock(env);
    const step3 = within(screen.getByRole("list", { name: "Steps" })).getAllByRole("listitem")[2]!;
    expect(within(step3).queryByRole("link")).toBeNull();
    expect(within(step3).getByRole("button", { name: "Mark done" })).not.toHaveAttribute("aria-disabled", "true");
  });
});

describe("commit tracker: Undo + rolled back", () => {
  it("every status has a text and a step count (rolled_back included)", () => {
    expect(COMMIT_STATUS_TEXT.rolled_back).toBe("Changes rolled back");
    expect(DONE_STEPS.rolled_back).toBe(4);
  });

  it("Undo asks for confirmation, then raises rollback with the change set id", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("commit_tracker.undo"));
    await user.click(screen.getByRole("button", { name: "Undo changes" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Undo these changes in Merxa?" });
    expect(dialog).toHaveTextContent("This restores the 9 changed records to how they were before.");
    // Cancel first: nothing is sent.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(onAction).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Undo changes" }));
    const again = await screen.findByRole("alertdialog");
    await user.click(within(again).getByRole("button", { name: "Undo changes" }));
    expect(onAction).toHaveBeenCalledWith({ type: "rollback", changeSetId: "cs_019a0c50" });
  });

  it("a disabled Undo says why, and conflicts are listed", async () => {
    const { container } = renderBlock(fixture("commit_tracker.conflicts"));
    const undo = screen.getByRole("button", { name: "Undo changes" });
    expect(undo).toHaveAttribute("aria-disabled", "true");
    expect(undo).toHaveAccessibleDescription("Merxa reports later changes on 2 of these records.");
    const conflicts = container.querySelector("[data-conflicts]")!;
    expect(conflicts).toHaveTextContent("These changes can't be undone: 2 records were changed again later in Merxa.");
    expect(conflicts).toHaveTextContent("TO-2026-0418");
    await expectNoA11yViolations(container);
  });

  it("busy disables Undo; no Undo without the engine's action", () => {
    renderBlock(fixture("commit_tracker.undo"), { busy: true });
    expect(screen.getByRole("button", { name: "Undo changes" })).toHaveAttribute("aria-disabled", "true");
    const plain = fixture("commit_tracker");
    const { container } = render(<BlockRenderer envelope={plain} onAction={() => {}} />);
    expect(within(container as HTMLElement).queryByRole("button", { name: "Undo changes" })).toBeNull();
  });

  it("rolled back: badge, all steps done incl. 'Rolled back', no Undo", async () => {
    const { container } = renderBlock(fixture("commit_tracker.rolled_back"));
    expect(container.querySelector('[data-status="rolled_back"]')).toHaveTextContent("Changes rolled back in Merxa");
    const steps = within(screen.getByRole("list", { name: "Commit progress" })).getAllByRole("listitem");
    expect(steps.map((s) => s.textContent)).toEqual(["Applying (done)", "Applied (done)", "Verified (done)", "Rolled back (done)"]);
    expect(screen.queryByRole("button", { name: "Undo changes" })).toBeNull();
    expect(undoAction({ status: "rolled_back", actions: [{ type: "undo", label: "Undo", enabled: true, disabled_reason: null }] })).toBeNull();
    await expectNoA11yViolations(container);
  });
});

describe("re-plan banner + plan versions", () => {
  const row = (seq: number, target: string, after: string, qty: number | null = 100, op = "transfer_order.create"): PlanDiffRow => ({
    seq,
    op,
    target_label: target,
    before_text: "No transfer",
    after_text: after,
    qty,
    system: "Merxa",
  });

  it("comparePlanDiffs: added / changed / removed / unchanged", () => {
    const v1 = [row(1, "A · DE1 → UK1", "Ship 600"), row(2, "B · DE1 → NL1", "Ship 300"), row(3, "CGP-UK1-SA W43", "Book 4", 4, "capacity_booking.create")];
    const v2 = [row(1, "A · DE1 → UK1", "Ship 600"), row(2, "B · DE1 → NL1", "Ship 450", 150), row(3, "C · DE1 → UK1", "Ship 900")];
    const c = comparePlanDiffs(v1, v2);
    expect(c.unchanged).toBe(1);
    expect(c.added.map((r) => r.target_label)).toEqual(["C · DE1 → UK1"]);
    expect(c.changed.map((x) => [x.previous.after_text, x.row.after_text])).toEqual([["Ship 300", "Ship 450"]]);
    expect(c.removed.map((r) => r.target_label)).toEqual(["CGP-UK1-SA W43"]);
    expect(comparisonSentence(c, 1)).toBe("Against v1: 1 added, 1 changed, 1 no longer in the plan");
    expect(comparisonSentence(comparePlanDiffs(v1, v1), 1)).toBe("Same changes as v1");
  });

  it("comparePlanDiffs on a delta: the applied rows of the version before are never 'no longer in the plan'", () => {
    // Regression (M5 review): a re-plan after a commit lists only the delta change set (95 §4),
    // so comparing it like a full plan claimed every applied change had been dropped.
    const v1 = [row(1, "A · DE1 → UK1", "Ship 600"), row(2, "CGP-UK1-SA W43", "Book 4", 4, "capacity_booking.create")];
    const v2 = [row(1, "CGP-UK1-SA W43", "Book 2", 2, "capacity_booking.create"), row(2, "C · DE1 → UK1", "Ship 900")];
    const c = comparePlanDiffs(v1, v2, { delta: true });
    expect(c.removed).toEqual([]);
    expect(c.added.map((r) => r.target_label)).toEqual(["C · DE1 → UK1"]);
    expect(c.changed.map((x) => [x.previous.after_text, x.row.after_text])).toEqual([["Book 4", "Book 2"]]);
    expect(comparisonSentence(c, 1, { delta: true })).toBe("On top of v1 (already applied): 1 added, 1 changed");
    expect(comparisonSentence(comparePlanDiffs(v1, [], { delta: true }), 1, { delta: true })).toBe("Nothing to change on top of v1");
    // a delta row identical to an applied one is still a change to make (never "nothing to change")
    const again = comparePlanDiffs(v1, [v1[0]!], { delta: true });
    expect(again.added).toHaveLength(1);
    expect(comparisonSentence(again, 1, { delta: true })).toBe("On top of v1 (already applied): 1 added");
    expect(isDeltaOf({ status: "committed" })).toBe(true);
    expect(isDeltaOf({ status: "superseded" })).toBe(false);
    expect(isDeltaOf(undefined)).toBe(false);
  });

  it("PlanVersionSwitcher compares a superseded (never applied) version as a full plan", () => {
    const v1 = { plan_version: 1, rows: [row(1, "A · DE1 → UK1", "Ship 600"), row(2, "CGP-UK1-SA W43", "Book 4", 4, "capacity_booking.create")], totals: { lines: 2, units: 604, value_cents: 100 } };
    const v2 = { plan_version: 2, rows: [row(1, "A · DE1 → UK1", "Ship 600"), row(2, "C · DE1 → UK1", "Ship 900")], totals: { lines: 2, units: 1500, value_cents: 200 } };
    const { container } = render(
      <PlanVersionSwitcher
        versions={[
          { version: 1, label: "Plan v1", status: "superseded", current: false, diff: v1, whatChanged: null },
          { version: 2, label: "Plan v2", status: "proposed", current: true, diff: v2, whatChanged: null },
        ]}
      />,
    );
    expect(container.querySelector("[data-plan-comparison]")).toHaveTextContent("Against v1: 1 added, 1 no longer in the plan");
  });

  it("the narrative block shows the banner and the versions", () => {
    const { container } = renderBlock(fixture("narrative.replan"));
    expect(container.querySelector("[data-replan-banner]")).toHaveTextContent(
      "New information: sea-air capacity on CGP-UK1-SA cut to 30% — plan updated (v2)",
    );
    expect(within(screen.getByRole("list", { name: "Plan versions" })).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Plan v1 · Committed",
      "Plan v2 · Proposed (current)",
    ]);
  });

  it("ReplanNotice offers Compare when asked", async () => {
    const user = userEvent.setup();
    const onCompare = vi.fn();
    const banner = (fixture("narrative.replan").props as { banner: { text: string; plan_version: number; previous_plan_version: number; signal_id: string | null } }).banner;
    render(<ReplanNotice banner={banner} onCompare={onCompare} />);
    await user.click(screen.getByRole("button", { name: "Compare v1 and v2" }));
    expect(onCompare).toHaveBeenCalledOnce();
  });

  it("PlanVersionSwitcher switches between v1 and v2 with the comparison and the diff table", async () => {
    const user = userEvent.setup();
    const v1 = { plan_version: 1, rows: [row(1, "A · DE1 → UK1", "Ship 600"), row(2, "CGP-UK1-SA W43", "Book 4", 4, "capacity_booking.create")], totals: { lines: 2, units: 604, value_cents: 100 } };
    const v2 = { plan_version: 2, rows: [row(1, "A · DE1 → UK1", "Ship 600"), row(2, "C · DE1 → UK1", "Ship 900")], totals: { lines: 2, units: 1500, value_cents: 200 } };
    const { container } = render(
      <PlanVersionSwitcher
        versions={[
          { version: 1, label: "Plan v1", status: "committed", current: false, diff: v1, whatChanged: null },
          { version: 2, label: "Plan v2", status: "proposed", current: true, diff: v2, whatChanged: "4 sea-air bookings → 2" },
        ]}
      />,
    );
    const group = screen.getByRole("group", { name: "Plan versions" });
    expect(within(group).getByRole("button", { name: /Plan v2/ })).toHaveAttribute("aria-pressed", "true");
    // v1 was committed: v2 lists the changes on top of it (a delta), nothing was "dropped".
    expect(container.querySelector("[data-plan-comparison]")).toHaveTextContent("On top of v1 (already applied): 2 added");
    expect(container.querySelector("[data-plan-comparison]")).not.toHaveTextContent("no longer in the plan");
    expect(container.querySelector("[data-what-changed]")).toHaveTextContent("4 sea-air bookings → 2");
    expect(screen.getByRole("table", { name: "Changes in Plan v2" })).toBeInTheDocument();
    await user.click(within(group).getByRole("button", { name: /Plan v1/ }));
    expect(within(group).getByRole("button", { name: /Plan v1/ })).toHaveAttribute("aria-pressed", "true");
    expect(container.querySelector("[data-plan-comparison]")).toBeNull();
    expect(screen.getByRole("table", { name: "Changes in Plan v1" })).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });
});
