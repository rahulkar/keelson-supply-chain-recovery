import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ACTIVITY_PREVIEW, ThinkingTimeline, agentsSummary, groupActivities, type ThinkingAgent, type ThinkingTimelineProps } from "../src";
import { expectNoA11yViolations } from "./a11y";
import { setMatchMedia } from "./setup";

const base: ThinkingTimelineProps = {
  status: "running",
  headline: "Checking your systems…",
  stages: [
    { id: "understanding", label: "Understanding", state: "done" },
    { id: "checking_systems", label: "Checking your systems", state: "active" },
    { id: "measuring_impact", label: "Measuring impact", state: "pending" },
    { id: "building_options", label: "Building options", state: "pending" },
    { id: "ready_for_review", label: "Ready for review", state: "pending" },
  ],
  agents: [
    {
      id: "inventory_demand",
      name: "Inventory & demand analyst",
      description: "Checks stock, allocations and forecast",
      state: "running",
      activities: [
        { id: "a1", label: "Read 12 open purchase orders from Merxa", state: "done" },
        { id: "a2", label: "Checking stock at UK1 and NL1", state: "running" },
      ],
    },
    {
      id: "sourcing",
      name: "Sourcing analyst",
      description: "Looks at suppliers and factories",
      state: "done",
      activities: [{ id: "b1", label: "Found 2 factories using the trim", state: "done" }],
    },
  ],
  reasoning: [{ agentId: "inventory_demand", text: "Comparing ETAs against the W45 peak." }],
  elapsedMs: 65_000,
};

describe("ThinkingTimeline", () => {
  it("is collapsed by default with a 'Show thinking' disclosure and a shimmering one-liner", () => {
    render(<ThinkingTimeline {...base} />);
    const trigger = screen.getByRole("button", { name: /show thinking/i });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Checking your systems…")).toHaveAttribute("data-motion", "on");
    expect(screen.queryByRole("list", { name: "Agents consulted" })).not.toBeInTheDocument();
    expect(screen.queryByText("Read 12 open purchase orders from Merxa")).not.toBeInTheDocument();
    expect(screen.getByText("1m 05s")).toBeInTheDocument();
    // Agents are summarised as a labelled avatar stack even when collapsed.
    expect(
      screen.getByRole("group", { name: "Agents consulted: Inventory & demand analyst, Sourcing analyst" }),
    ).toBeInTheDocument();
  });

  it("expands to stages and agents; agent rows reveal activity sentences and reasoning", async () => {
    const user = userEvent.setup();
    render(<ThinkingTimeline {...base} />);
    await user.click(screen.getByRole("button", { name: /show thinking/i }));
    expect(screen.getByRole("button", { name: /hide thinking/i })).toHaveAttribute("aria-expanded", "true");

    const progress = screen.getByRole("list", { name: "Progress" });
    const current = within(progress)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("aria-current") === "step");
    expect(current).toHaveTextContent("Checking your systems");

    const agents = screen.getByRole("list", { name: "Agents consulted" });
    expect(within(agents).getAllByRole("listitem")).toHaveLength(2);
    // Activities are collapsed per agent until opened.
    expect(screen.queryByText("Read 12 open purchase orders from Merxa")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Inventory & demand analyst/ }));
    expect(screen.getByRole("list", { name: "Inventory & demand analyst activity" })).toBeInTheDocument();
    expect(screen.getByText("Read 12 open purchase orders from Merxa")).toBeInTheDocument();
    expect(screen.getByText("Comparing ETAs against the W45 peak.")).toBeInTheDocument();
    // Plain business language: no tool names, no durations per activity.
    expect(screen.queryByText(/list_purchase_orders/)).not.toBeInTheDocument();
  });

  it("announces stage changes (only) via a polite live region", async () => {
    const { rerender } = render(<ThinkingTimeline {...base} />);
    const live = screen.getByTestId("thinking-live");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveAttribute("role", "status");
    expect(live).toHaveTextContent("Checking your systems");

    // New activity, same stage → no new announcement.
    const moreActivity = structuredClone(base);
    moreActivity.agents[0]!.activities.push({ id: "a3", label: "Read the demand forecast", state: "running" });
    rerender(<ThinkingTimeline {...moreActivity} />);
    expect(live).toHaveTextContent(/^Checking your systems$/);

    const next = structuredClone(base);
    next.stages[1]!.state = "done";
    next.stages[2]!.state = "active";
    rerender(<ThinkingTimeline {...next} headline="Measuring impact…" />);
    expect(live).toHaveTextContent(/^Measuring impact$/);
    // The live region never contains the headline/activities.
    expect(live).not.toHaveTextContent("…");
  });

  it("does not re-announce a stage when it flips from active to done", () => {
    const { rerender } = render(<ThinkingTimeline {...base} />);
    const live = screen.getByTestId("thinking-live");
    const settled = structuredClone(base);
    settled.stages[1]!.state = "done";
    rerender(<ThinkingTimeline {...settled} />);
    expect(live).toHaveTextContent(/^Checking your systems$/);
    // The next stage starting is announced.
    const next = structuredClone(settled);
    next.stages[2]!.state = "active";
    rerender(<ThinkingTimeline {...next} />);
    expect(live).toHaveTextContent(/^Measuring impact$/);
  });

  it("stays silent on mount for runs that are not live (thread reload, earlier turns)", () => {
    const done = structuredClone(base);
    for (const s of done.stages) s.state = "done";
    const { rerender } = render(<ThinkingTimeline {...done} status="done" headline="Ready for review" />);
    const live = screen.getByTestId("thinking-live");
    expect(live).toBeEmptyDOMElement();
    rerender(<ThinkingTimeline {...done} status="done" headline="Ready for review" />);
    expect(live).toBeEmptyDOMElement();
    // A paused run that later moves on is announced from then on.
    const { rerender: rerenderPaused } = render(<ThinkingTimeline {...base} status="paused" />);
    const lives = screen.getAllByTestId("thinking-live");
    const pausedLive = lives[lives.length - 1]!;
    expect(pausedLive).toBeEmptyDOMElement();
    const resumed = structuredClone(base);
    resumed.stages[1]!.state = "done";
    resumed.stages[2]!.state = "active";
    rerenderPaused(<ThinkingTimeline {...resumed} />);
    expect(pausedLive).toHaveTextContent(/^Measuring impact$/);
  });

  it("offers Stop while running", async () => {
    const onStop = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<ThinkingTimeline {...base} onStop={onStop} />);
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
    rerender(<ThinkingTimeline {...base} status="done" onStop={onStop} />);
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("does not shimmer when finished, paused or errored and spells the status out", () => {
    const { rerender } = render(<ThinkingTimeline {...base} status="done" headline="Ready for review" />);
    expect(screen.getByText("Ready for review")).not.toHaveAttribute("data-motion");
    expect(screen.getByText(/^Done:/)).toBeInTheDocument();
    rerender(<ThinkingTimeline {...base} status="paused" headline="Waiting for your input" />);
    expect(screen.getByText(/^Waiting for you:/)).toBeInTheDocument();
    rerender(<ThinkingTimeline {...base} status="error" headline="Couldn't reach Merxa" />);
    expect(screen.getByText(/^Something went wrong:/)).toBeInTheDocument();
  });

  it("disables the shimmer under prefers-reduced-motion", () => {
    setMatchMedia((q) => q.includes("prefers-reduced-motion"));
    render(<ThinkingTimeline {...base} />);
    const headline = screen.getByText("Checking your systems…");
    expect(headline).toHaveAttribute("data-motion", "reduced");
    expect(headline.className).not.toContain("k-shimmer-text");
  });

  it("honours defaultOpen", () => {
    render(<ThinkingTimeline {...base} defaultOpen />);
    expect(screen.getByRole("list", { name: "Agents consulted" })).toBeInTheDocument();
  });

  it("has no axe violations collapsed or expanded", async () => {
    const user = userEvent.setup();
    const { container } = render(<ThinkingTimeline {...base} onStop={() => {}} />);
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: /show thinking/i }));
    await user.click(screen.getByRole("button", { name: /Sourcing analyst/ }));
    await act(async () => {});
    await expectNoA11yViolations(container);
  });

  it("is a region named \"Thinking\" by default, or by its label prop", () => {
    const { unmount } = render(<ThinkingTimeline {...base} />);
    expect(screen.getByRole("region", { name: "Thinking" })).toHaveAttribute("data-status", "running");
    unmount();
    render(<ThinkingTimeline {...base} label="Thinking, turn 3" />);
    expect(screen.getByRole("region", { name: "Thinking, turn 3" })).toBeInTheDocument();
  });

  it("several timelines with unique labels pass axe landmark-unique (regression)", async () => {
    const { container } = render(
      <>
        <ThinkingTimeline {...base} status="done" label="Thinking, turn 1" />
        <ThinkingTimeline {...base} label="Thinking, turn 2" />
      </>,
    );
    expect(screen.getAllByRole("region").map((r) => r.getAttribute("aria-label"))).toEqual([
      "Thinking, turn 1",
      "Thinking, turn 2",
    ]);
    await expectNoA11yViolations(container); // best-practice rules incl. landmark-unique
  });
});

describe("ThinkingTimeline with many agents (M4 parallel specialists)", () => {
  const agent = (id: string, name: string, state: ThinkingAgent["state"], activities: ThinkingAgent["activities"] = []): ThinkingAgent => ({
    id,
    name,
    description: "",
    state,
    activities,
  });
  const many: ThinkingTimelineProps = {
    ...base,
    agents: [
      agent("planner", "Planner", "done"),
      agent("logistics", "Logistics analyst", "running", [
        { id: "l1", label: "Read supplier messages", state: "done" },
        { id: "l2", label: "Read supplier messages", state: "error" },
        { id: "l3", label: "Read supplier messages", state: "done" },
        ...Array.from({ length: 8 }, (_, i) => ({ id: `s${i}`, label: `Checked sailing ${i + 1}`, state: "done" as const })),
        { id: "now", label: "Checking alternative lanes", state: "running" },
      ]),
      agent("sourcing", "Sourcing analyst", "error"),
      agent("inventory_demand", "Inventory & demand analyst", "running"),
      agent("playbook", "Playbook analyst", "done"),
    ],
  };

  it("collapses the avatar stack to three with '+N' and still names every agent", () => {
    render(<ThinkingTimeline {...many} />);
    const group = screen.getByRole("group", {
      name: "Agents consulted: Planner, Logistics analyst, Sourcing analyst, Inventory & demand analyst, Playbook analyst",
    });
    expect(group).toHaveTextContent("+2");
    expect(group.querySelector("[data-overflow]")).toHaveAttribute("title", "Inventory & demand analyst, Playbook analyst");
  });

  it("summarises the agents and groups repeated activities; the latest steps stay visible", async () => {
    const user = userEvent.setup();
    const { container } = render(<ThinkingTimeline {...many} defaultOpen />);
    expect(container.querySelector("[data-agents-summary]")).toHaveTextContent("5 agents · 2 working · 1 ran into a problem");
    await user.click(screen.getByRole("button", { name: /Logistics analyst/ }));
    const list = screen.getByRole("list", { name: "Logistics analyst activity" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(ACTIVITY_PREVIEW);
    // Regression: the step in progress (the newest) is shown, not folded behind "Show more".
    expect(rows.at(-1)).toHaveTextContent("Checking alternative lanes");
    expect(rows.at(-1)).toHaveAttribute("data-state", "running");
    expect(screen.getByRole("button", { name: /Logistics analyst/ })).toHaveTextContent("12 steps");
    const more = screen.getByRole("button", { name: "Show 4 earlier steps" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await user.click(more);
    const all = within(list).getAllByRole("listitem");
    expect(all).toHaveLength(10);
    expect(all[0]).toHaveTextContent("Read supplier messages · 3 times");
    expect(all[0]).toHaveAttribute("data-state", "error");
    expect(screen.getByRole("button", { name: "Show fewer steps" })).toHaveAttribute("aria-expanded", "true");
    await expectNoA11yViolations(container);
  });

  it("has no summary line for up to three agents", () => {
    const { container } = render(<ThinkingTimeline {...base} defaultOpen />);
    expect(container.querySelector("[data-agents-summary]")).toBeNull();
  });

  it("helpers", () => {
    expect(
      groupActivities([
        { id: "1", label: "A", state: "done" },
        { id: "2", label: "A", state: "error" },
        { id: "3", label: "B", state: "done" },
        { id: "4", label: "A", state: "done" },
      ]),
    ).toEqual([
      { id: "1", label: "A", state: "error", count: 2 },
      { id: "3", label: "B", state: "done", count: 1 },
      { id: "4", label: "A", state: "done", count: 1 },
    ]);
    expect(agentsSummary([{ state: "done" }, { state: "done" }, { state: "done" }, { state: "done" }])).toBe("4 agents · all finished");
  });
});
