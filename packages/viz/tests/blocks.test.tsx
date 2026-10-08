import { knownBlockEnvelopeSchema, type BlockEnvelope } from "@keelson/contracts";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { z } from "zod";
import { describe, expect, it, onTestFinished, vi } from "vitest";
import {
  BlockRenderer,
  approvalConnectState,
  blockRegistry,
  registerBlock,
  type BlockAction,
  type BlockViewer,
  type ViewerConnection,
} from "../src";
import { splitLongText } from "../src/blocks/PlanDiff";
import { blockFixtures, getFixture } from "../src/fixtures";
import { expectNoA11yViolations } from "./a11y";

const fixture = (name: string): BlockEnvelope => {
  const f = getFixture(name);
  if (!f) throw new Error(`missing fixture ${name}`);
  return structuredClone(f.envelope);
};

const renderBlock = (envelope: BlockEnvelope, extra: { busy?: boolean; density?: "inline" | "canvas" } = {}) => {
  const onAction = vi.fn<(a: BlockAction) => Promise<void>>().mockResolvedValue(undefined);
  const utils = render(<BlockRenderer envelope={envelope} onAction={onAction} {...extra} />);
  return { ...utils, onAction };
};

describe("fixtures", () => {
  it("cover every registered block kind (M1 + M3)", () => {
    const kinds = new Set(blockFixtures.filter((f) => !f.expectFallback).map((f) => `${f.envelope.kind}@${f.envelope.v}`));
    for (const key of Object.keys(blockRegistry)) expect(kinds, key).toContain(key);
  });

  for (const f of blockFixtures) {
    it(`${f.name}: ${f.expectFallback ? "renders as RawDataCard" : "renders its block"}`, async () => {
      if (!f.expectFallback && f.name !== "plan_diff.large" && f.name !== "network_map.large") {
        // Valid fixtures satisfy the strict contract union exactly.
        expect(knownBlockEnvelopeSchema.safeParse(f.envelope).success).toBe(true);
      }
      const onAction = vi.fn<(a: BlockAction) => Promise<void>>().mockResolvedValue(undefined);
      const { container } = render(
        <BlockRenderer
          envelope={f.envelope}
          onAction={onAction}
          data={f.data}
          density={f.envelope.placement === "canvas" ? "canvas" : "inline"}
        />,
      );
      if (f.expectFallback) {
        await waitFor(() => expect(container.querySelector("[data-block-fallback]")).not.toBeNull());
      } else {
        // M3 canvas visuals load on demand (lazy), M1 blocks render synchronously.
        await waitFor(() => expect(container.querySelector(`[data-block-id="${f.envelope.id}"]`)).not.toBeNull(), { timeout: 10_000 });
        expect(container.querySelector("[data-block-fallback]")).toBeNull();
      }
    });
  }

  it("valid fixtures have no axe violations", async () => {
    for (const f of blockFixtures.filter((x) => !x.expectFallback && x.name !== "plan_diff.large")) {
      const { container, unmount } = render(<BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} density="canvas" />);
      await waitFor(() => expect(container.querySelector(`[data-block-id="${f.envelope.id}"]`)).not.toBeNull(), { timeout: 10_000 });
      await expectNoA11yViolations(container);
      unmount();
    }
  }, 60_000);
});

describe("BlockRenderer fallbacks", () => {
  const invalid: [string, (e: BlockEnvelope) => void][] = [
    ["kpi_strip with float value", (e) => ((e.props as { items: { value: number }[] }).items[0]!.value = 1.5)],
    ["approach_card without interrupt_id", (e) => delete (e.props as Record<string, unknown>)["interrupt_id"]],
    ["recommended_option with unknown action", (e) => ((e.props as { actions: { type: string }[] }).actions[0]!.type = "buy")],
    ["plan_diff with seq 0", (e) => ((e.props as { rows: { seq: number }[] }).rows[0]!.seq = 0)],
    ["approval_card with short hash", (e) => ((e.props as { change_set_hash: string }).change_set_hash = "abc")],
    ["commit_tracker with unknown status", (e) => ((e.props as { status: string }).status = "done")],
    ["narrative with empty markdown", (e) => ((e.props as { markdown: string }).markdown = "")],
    ["evidence_pack with bad evidence id", (e) => ((e.props as { items: { id: string }[] }).items[0]!.id = "evidence-1")],
    ["kpi_strip with extra key", (e) => ((e.props as Record<string, unknown>)["extra"] = true)],
  ];
  const sourceFor: Record<string, string> = {
    kpi_strip: "kpi_strip",
    approach_card: "approach_card",
    recommended_option: "recommended_option",
    plan_diff: "plan_diff",
    approval_card: "approval_card",
    commit_tracker: "commit_tracker",
    narrative: "narrative",
    evidence_pack: "evidence_pack",
  };

  it.each(invalid)("%s → RawDataCard with issues", (name, mutate) => {
    const kind = Object.keys(sourceFor).find((k) => name.startsWith(k))!;
    const env = fixture(sourceFor[kind]!);
    mutate(env);
    const { container } = renderBlock(env);
    const card = container.querySelector('[data-block-fallback="invalid_props"]');
    expect(card).not.toBeNull();
    expect(card).toHaveAttribute("data-block-key", `${kind}@1`);
    expect(within(card as HTMLElement).getByRole("list", { name: "Problems" })).toBeInTheDocument();
  });

  it("unknown kind and wrong version → RawDataCard (unknown_kind)", () => {
    const env = fixture("kpi_strip");
    env.v = 2;
    const { container } = renderBlock(env);
    expect(container.querySelector('[data-block-fallback="unknown_kind"]')).toHaveAttribute("data-block-key", "kpi_strip@2");
  });

  it("non-envelope input → RawDataCard (not_an_envelope)", () => {
    const { container } = renderBlock({ hello: "world" } as unknown as BlockEnvelope);
    expect(container.querySelector('[data-block-fallback="not_an_envelope"]')).not.toBeNull();
  });

  it("render errors are contained → RawDataCard (render_error)", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    registerBlock("exploding@1", {
      schema: z.object({ boom: z.boolean() }),
      component: () => {
        throw new Error("kaboom");
      },
    });
    const { container } = renderBlock({ ...fixture("kpi_strip"), kind: "exploding", props: { boom: true } });
    expect(container.querySelector('[data-block-fallback="render_error"]')).toHaveTextContent("kaboom");
    spy.mockRestore();
  });

  it("RawDataCard keeps the data inspectable", async () => {
    const user = userEvent.setup();
    renderBlock(fixture("unknown.network_map"));
    await user.click(screen.getByText("Show data"));
    expect(screen.getByText(/"nodes"/)).toBeInTheDocument();
    // The scrollable JSON is reachable by keyboard.
    expect(screen.getByText(/"nodes"/).closest("pre")).toHaveAttribute("tabindex", "0");
  });
});

describe("KpiStrip", () => {
  it("formats values and shows provenance chips only on non-facts", () => {
    const env = fixture("kpi_strip");
    (env.props as { items: { prov: string }[] }).items[2]!.prov = "fact";
    const { container } = renderBlock(env);
    expect(screen.getByText("18,400 units")).toBeInTheDocument();
    expect(screen.getByText("€247,600")).toBeInTheDocument();
    expect(screen.getByText("21 days")).toBeInTheDocument();
    const factTile = container.querySelector('[data-kpi="stores_short"]')!;
    expect(factTile.querySelector("[data-prov]")).toBeNull();
    expect(container.querySelector('[data-kpi="units_at_risk"] [data-prov="simulated"]')).not.toBeNull();
    expect(screen.getByText("Preliminary · based on priors")).toBeInTheDocument();
  });

  it("shows signed deltas", () => {
    renderBlock(fixture("kpi_strip.final"));
    expect(screen.getByText("−4,800 units")).toBeInTheDocument();
    expect(screen.getByText("−€64,800")).toBeInTheDocument();
  });

  // Browser e2e finding: next to the open canvas (1280 px) the tiles used viewport breakpoints,
  // squeezed four columns into a ~460 px card and cut labels off ("Units recove…").
  it("sizes columns by its container and never truncates labels", () => {
    const { container } = renderBlock(fixture("kpi_strip"));
    const dl = container.querySelector("dl")!;
    expect(dl.parentElement).toHaveClass("@container");
    // Four tiles: 2 × 2 until all four fit in one row; no viewport breakpoints.
    expect(dl).toHaveClass("grid-cols-2", "@2xl:grid-cols-4");
    expect(dl.className).not.toMatch(/(^|\s)(sm|md|lg|xl):grid-cols/);
    expect(dl.className).not.toContain("@md:grid-cols-3");
    for (const dt of container.querySelectorAll("dt")) expect(dt).not.toHaveClass("truncate");
    expect(screen.getByText("18,400 units")).toHaveClass("whitespace-nowrap");
  });

  it("uses three columns for three tiles when there is room", () => {
    const { container } = renderBlock(fixture("recommended_option"));
    expect(container.querySelector("dl.grid")).toHaveClass("grid-cols-2", "@md:grid-cols-3");
  });
});

describe("ApproachCard", () => {
  it("emits interrupt decisions", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("approach_card"));
    const group = screen.getByRole("group", { name: "Approach decision" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Looks right",
      "Edit",
      "Treat as what-if",
      "Cancel",
    ]);
    await user.click(screen.getByRole("button", { name: "Looks right" }));
    expect(onAction).toHaveBeenCalledWith({ type: "interrupt_decision", interruptId: "int_019a0c4e_approach", decision: "confirm" });
  });

  it("collects free-text edits", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("approach_card"));
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByLabelText("What should I change?"), "Only PO-BD-0002 is affected");
    await user.click(screen.getByRole("button", { name: "Send changes" }));
    expect(onAction).toHaveBeenCalledWith({
      type: "interrupt_decision",
      interruptId: "int_019a0c4e_approach",
      decision: "edit",
      payload: { edits: "Only PO-BD-0002 is affected" },
    });
  });

  it("lists matched records and assumptions with provenance", async () => {
    const user = userEvent.setup();
    const { container } = renderBlock(fixture("approach_card"));
    await user.click(screen.getByText("9 matched records"));
    expect(within(screen.getByRole("region", { name: "Matched records" })).getByText("TRIM-BD-01")).toBeInTheDocument();
    expect(screen.getByText("and 5 more")).toBeInTheDocument();
    expect(container.querySelector('[data-prov="assumption"]')).not.toBeNull();
  });

  it("moves focus into the edit box and back to Edit (focused button is unmounted)", async () => {
    const user = userEvent.setup();
    renderBlock(fixture("approach_card"));
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("What should I change?")).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();
  });

  it("disables decisions while busy", () => {
    renderBlock(fixture("approach_card"), { busy: true });
    expect(screen.getByRole("button", { name: "Looks right" })).toHaveAttribute("aria-disabled", "true");
  });
});

describe("RecommendedOption", () => {
  it("raises case actions with option id and plan version", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("recommended_option"));
    expect(screen.getByText("Rebalance stock from DE1 to UK1 and NL1")).toBeInTheDocument();
    expect(screen.getByText("Air-freight trims to Dhaka")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply changes for me" }));
    expect(onAction).toHaveBeenCalledWith({ type: "case_action", action: "apply", optionId: "opt_rebalance", planVersion: 2 });
    await user.click(screen.getByRole("button", { name: "Guide me" }));
    expect(onAction).toHaveBeenLastCalledWith({ type: "case_action", action: "guide", optionId: "opt_rebalance", planVersion: 2 });
  });

  it("explains disabled actions", () => {
    const env = fixture("recommended_option");
    const actions = (env.props as { actions: { enabled: boolean; disabled_reason: string | null }[] }).actions;
    actions[0]!.enabled = false;
    actions[0]!.disabled_reason = "Connect Merxa to apply changes";
    renderBlock(env);
    const btn = screen.getByRole("button", { name: "Apply changes for me" });
    expect(btn).toHaveAttribute("aria-disabled", "true");
    expect(btn).toHaveAccessibleDescription("Apply changes for me: Connect Merxa to apply changes");
  });
});

describe("PlanDiff", () => {
  it("canvas density shows every row with totals", () => {
    renderBlock(fixture("plan_diff"), { density: "canvas" });
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 9);
    expect(screen.getByText("5,160 units")).toBeInTheDocument();
    expect(screen.getByText("€69,660")).toBeInTheDocument();
    expect(within(table).getAllByText("Create transfer order").length).toBe(8);
  });

  it("inline density shows 5 rows and opens the canvas for the rest", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("plan_diff"), { density: "inline" });
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(1 + 5);
    await user.click(screen.getByRole("button", { name: "View all 9 changes" }));
    expect(onAction).toHaveBeenCalledWith({ type: "open_canvas", blockId: "blk_plan_diff_v2" });
  });

  it("collapses a long before/after list to a preview with Show all (browser e2e finding)", async () => {
    const user = userEvent.setup();
    const env = fixture("plan_diff");
    const lines = Array.from({ length: 30 }, (_, i) => `PUF-M-BLK-${i} × ${i + 1}`);
    (env.props as { rows: { after_text: string }[] }).rows[0]!.after_text = lines.join(", ");
    renderBlock(env, { density: "canvas" });
    const toggle = screen.getByRole("button", { name: "Show all (27 more)" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText(`${lines.slice(0, 3).join(", ")}…`)).toBeInTheDocument();
    expect(screen.queryByText(lines.join(", "))).not.toBeInTheDocument();
    await user.click(toggle);
    expect(screen.getByText(lines.join(", "))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
    // Short texts stay as they are.
    expect(splitLongText("Ship 600 on 12 Oct")).toBeNull();
    expect(splitLongText("x".repeat(200))?.unit).toBe("chars");
  });

  it("sorts by column", async () => {
    const user = userEvent.setup();
    renderBlock(fixture("plan_diff"), { density: "canvas" });
    await user.click(screen.getByRole("button", { name: /Qty/ }));
    const header = screen.getByRole("columnheader", { name: /Qty/ });
    expect(header).toHaveAttribute("aria-sort");
    expect(header.getAttribute("aria-sort")).not.toBe("none");
  });

  it("the table scroller is a labelled, keyboard-focusable region (phones scroll sideways)", () => {
    renderBlock(fixture("plan_diff"), { density: "inline" });
    const region = screen.getByRole("region", { name: /Changes in Merxa \(9 lines\)/ });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(within(region).getByRole("table")).toBeInTheDocument();
  });

  it("virtualises more than 200 rows", () => {
    // jsdom has no layout: give the scroll container a real viewport for the virtualiser.
    const h = vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(480);
    const w = vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(960);
    onTestFinished(() => {
      h.mockRestore();
      w.mockRestore();
    });
    const { container } = renderBlock(getFixture("plan_diff.large")!.envelope, { density: "canvas" });
    const table = screen.getByRole("table");
    expect(container.querySelector("[data-virtualized]")).not.toBeNull();
    expect(table).toHaveAttribute("aria-rowcount", "251");
    const rendered = within(table).getAllByRole("row").length;
    expect(rendered).toBeGreaterThan(5);
    expect(rendered).toBeLessThan(100);
  });
});

describe("ApprovalCard", () => {
  it("raises a decision once even when clicked twice before React re-renders", async () => {
    let settle: () => void = () => {};
    const onAction = vi.fn<(a: BlockAction) => Promise<void>>(
      () => new Promise<void>((resolve) => (settle = resolve)),
    );
    render(<BlockRenderer envelope={fixture("approval_card.connected")} onAction={onAction} />);
    const approve = screen.getByRole("button", { name: "Approve" });
    act(() => {
      approve.click();
      approve.click();
    });
    expect(onAction).toHaveBeenCalledTimes(1);
    await act(async () => settle());
    // Once settled, a new decision can be raised again (M5: Reject asks for an optional reason first).
    act(() => screen.getByRole("button", { name: "Reject" }).click());
    act(() => screen.getByRole("button", { name: "Reject changes" }).click());
    expect(onAction).toHaveBeenCalledTimes(2);
  });

  it("requires Connect Merxa before Approve when not linked", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/cases/019a0c4e?x=1");
    const { onAction } = renderBlock(fixture("approval_card"));
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(approve).toHaveAccessibleDescription(/Connect your Merxa account/);
    await user.click(approve);
    expect(onAction).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Connect Merxa" }));
    expect(onAction).toHaveBeenCalledWith({ type: "connect", connector: "merxa", returnTo: "/cases/019a0c4e?x=1" });
  });

  it("offers Reconnect when the link expired", () => {
    const env = fixture("approval_card");
    (env.props as { connect: { status: string } }).connect.status = "expired";
    renderBlock(env);
    expect(screen.getByRole("button", { name: "Reconnect Merxa" })).toBeInTheDocument();
  });

  it("approves with plan version and change-set hash when linked", async () => {
    const user = userEvent.setup();
    const env = fixture("approval_card.connected");
    const hash = (env.props as { change_set_hash: string }).change_set_hash;
    const { onAction } = renderBlock(env);
    expect(screen.queryByRole("button", { name: /Connect Merxa/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAction).toHaveBeenCalledWith({
      type: "interrupt_decision",
      interruptId: "int_019a0c4e_approval",
      decision: "approve",
      payload: { plan_version: 2, change_set_hash: hash },
    });
    await user.click(screen.getByRole("button", { name: "Reject" }));
    // M5: an optional reason first; none given → no `reason` in the payload.
    await user.click(screen.getByRole("button", { name: "Reject changes" }));
    expect(onAction).toHaveBeenLastCalledWith({
      type: "interrupt_decision",
      interruptId: "int_019a0c4e_approval",
      decision: "reject",
      payload: { plan_version: 2 },
    });
    expect(screen.getByText("Approver")).toBeInTheDocument();
  });
});

describe("ApprovalCard — the viewer's Merxa link decides (regression)", () => {
  const viewer = (merxa: ViewerConnection): BlockViewer => ({ connections: { merxa } });
  const renderWith = (name: string, v: BlockViewer) => {
    const onAction = vi.fn<(a: BlockAction) => Promise<void>>().mockResolvedValue(undefined);
    const utils = render(<BlockRenderer envelope={fixture(name)} onAction={onAction} viewer={v} />);
    return { ...utils, onAction };
  };

  it("an initiator-connected card still asks an unlinked viewer to Connect Merxa", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/cases/019a0c4e");
    const { onAction } = renderWith("approval_card.connected", viewer({ status: "missing", applyReady: false }));
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(approve).toHaveAccessibleDescription(/Connect your Merxa account/);
    await user.click(approve);
    expect(onAction).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Connect Merxa" }));
    expect(onAction).toHaveBeenCalledWith({ type: "connect", connector: "merxa", returnTo: "/cases/019a0c4e" });
  });

  it("an unlinked-initiator card lets a linked viewer approve", async () => {
    const user = userEvent.setup();
    const { onAction } = renderWith("approval_card", viewer({ status: "active", applyReady: true }));
    expect(screen.queryByRole("button", { name: /Connect Merxa/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({ type: "interrupt_decision", decision: "approve" }));
  });

  it("waits (Approve disabled, no call to action) while the link is being checked", () => {
    renderWith("approval_card.connected", viewer({ status: "checking" }));
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(approve).toHaveAccessibleDescription("Checking your Merxa connection…");
    expect(screen.queryByRole("button", { name: /Connect Merxa/ })).not.toBeInTheDocument();
    // Reject does not need a Merxa link
    expect(screen.getByRole("button", { name: "Reject" })).not.toHaveAttribute("aria-disabled", "true");
  });

  it("offers Reconnect for an expired link and for an active link without changes:apply", () => {
    const { unmount } = renderWith("approval_card.connected", viewer({ status: "expired", applyReady: false }));
    expect(screen.getByRole("button", { name: "Reconnect Merxa" })).toBeInTheDocument();
    expect(screen.getByText(/has expired/)).toBeInTheDocument();
    unmount();
    renderWith("approval_card.connected", viewer({ status: "active", applyReady: false }));
    expect(screen.getByRole("button", { name: "Reconnect Merxa" })).toBeInTheDocument();
    expect(screen.getByText(/doesn't allow applying changes/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toHaveAttribute("aria-disabled", "true");
  });

  it("when the check failed, Approve is offered and the server's 412 decides", () => {
    renderWith("approval_card", viewer({ status: "unknown" }));
    expect(screen.getByRole("button", { name: "Approve" })).not.toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByRole("button", { name: /Connect Merxa/ })).not.toBeInTheDocument();
  });

  it("approvalConnectState falls back to the block's prop without a viewer", () => {
    expect(approvalConnectState({ status: "active", connect_url: null }, undefined)).toBe("ready");
    expect(approvalConnectState({ status: "expired", connect_url: null }, undefined)).toBe("reconnect");
    expect(approvalConnectState({ status: "missing", connect_url: null }, {})).toBe("connect");
    expect(approvalConnectState({ status: "active", connect_url: null }, viewer({ status: "missing", applyReady: false }))).toBe("connect");
  });

  it("has no axe violations in each viewer state", async () => {
    for (const v of [viewer({ status: "checking" }), viewer({ status: "missing", applyReady: false }), viewer({ status: "active", applyReady: true })]) {
      const { container, unmount } = renderWith("approval_card", v);
      await expectNoA11yViolations(container);
      unmount();
    }
  });
});

describe("CommitTracker", () => {
  it("shows verified state with a deep link to Merxa", () => {
    const { container } = renderBlock(fixture("commit_tracker"));
    expect(screen.getByText("Changes applied and verified")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Open in Merxa/ });
    expect(link).toHaveAttribute("href", "http://merxa.localhost/change-batches/CB-2026-0042");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(container.querySelector('[data-prov="verified"]')).not.toBeNull();
  });

  it("marks the current step while applying", () => {
    renderBlock(fixture("commit_tracker.applying"));
    const progress = screen.getByRole("list", { name: "Commit progress" });
    const current = within(progress)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("aria-current") === "step");
    expect(current).toHaveTextContent("Applying");
  });

  it.each([
    ["applying", ["in progress", "not started", "not started"]],
    ["applied", ["done", "done", "in progress"]],
    ["verified", ["done", "done", "done"]],
    ["conflict", ["done", "failed", "not started"]],
  ] as const)("status %s → step states %j", (status, expected) => {
    const env = fixture("commit_tracker");
    (env.props as { status: string }).status = status;
    renderBlock(env);
    const steps = within(screen.getByRole("list", { name: "Commit progress" })).getAllByRole("listitem");
    expect(steps.map((li) => /\((.+)\)/.exec(li.textContent ?? "")?.[1])).toEqual(expected);
    const current = steps.filter((li) => li.getAttribute("aria-current") === "step");
    expect(current).toHaveLength(status === "verified" ? 0 : 1);
  });

  it("drops deep links to non-allowlisted hosts", () => {
    const env = fixture("commit_tracker");
    (env.props as { ext_url: string }).ext_url = "http://evil.example.com/change-batches/CB-1";
    renderBlock(env);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("Narrative (Streamdown, hardened)", () => {
  it("renders markdown with allowlisted links", () => {
    renderBlock(fixture("narrative"));
    const strong = screen.getByText("Three purchase orders slip into peak.");
    expect(strong.closest("li")).not.toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByRole("link", { name: /change batch in Merxa/ })).toHaveAttribute(
      "href",
      "http://merxa.localhost/change-batches/CB-2026-0042",
    );
  });

  it("strips raw HTML, images and non-allowlisted links", () => {
    const env = fixture("narrative");
    (env.props as { markdown: string }).markdown = [
      "Read [the docs](https://evil.example.com/phish) and [case](http://keelson.localhost/cases/1).",
      "",
      "![tracker](https://evil.example.com/pixel.png)",
      "",
      '<img src="https://evil.example.com/x.png" onerror="alert(1)"><script>alert(1)</script>',
      "",
      "[js](javascript:alert(1))",
      "",
      "[reconnect](http://keelson.localhost/api/connectors/merxa/connect?return_to=https://evil.example.com)",
    ].join("\n");
    const { container } = renderBlock(env);
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "http://keelson.localhost/cases/1");
    expect(screen.getByText("the docs")).toBeInTheDocument();
    // An allowlisted host's API endpoint is shown as text, never as a link.
    expect(screen.getByText("reconnect").closest("a")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.innerHTML).not.toContain("javascript:");
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelector("iframe, object, embed")).toBeNull();
  });
});

describe("EvidencePack", () => {
  it("groups by source, shows every provenance and filters", async () => {
    const user = userEvent.setup();
    const { container } = renderBlock(fixture("evidence_pack"), { density: "canvas" });
    expect(screen.getByRole("region", { name: "From Merxa" })).toBeInTheDocument();
    expect(container.querySelectorAll('[data-prov="fact"]').length).toBe(3);
    expect(screen.getAllByText(/Retrieved 5 Oct 2026, 09:12 UTC/).length).toBe(4);
    await user.click(screen.getByRole("button", { name: /simulated/i }));
    expect(container.querySelectorAll("li[id^=ev_]").length).toBe(1);
  });
});
