import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  AvatarStack,
  Badge,
  Button,
  CanvasPanel,
  Card,
  CardHeader,
  Chip,
  Dialog,
  EmptyState,
  IconButton,
  MessageBubble,
  ProvenanceChip,
  StageChips,
  Tabs,
  Toaster,
  createBufferedToastManager,
  UiProvider,
  formatElapsed,
  initials,
  toast,
} from "../src";
import { expectNoA11yViolations } from "./a11y";
import { setMatchMedia } from "./setup";

describe("ProvenanceChip", () => {
  const cases = [
    ["fact", "Fact", "solid"],
    ["inference", "Inferred", "dashed"],
    ["assumption", "Assumption", "dotted"],
    ["simulated", "Simulated", "hatched"],
    ["decision", "Decision", "transparent"],
    ["committed", "Committed", "double"],
  ] as const;
  it.each(cases)("%s → colour + icon + %s border", (prov, label, border) => {
    const { container } = render(<ProvenanceChip prov={prov} />);
    const chip = container.querySelector(`[data-prov="${prov}"]`)!;
    expect(chip).toHaveTextContent(`Provenance: ${label}`);
    expect(chip.querySelector("svg")).not.toBeNull();
    expect(chip.className).toContain(`text-prov-${prov}`);
    expect(chip.className).toContain(border === "hatched" ? "k-prov-hatched" : `border-${border}`);
  });

  it("verified = committed style + badge + double-check icon", () => {
    const { container } = render(<ProvenanceChip prov="committed" verified />);
    const chip = container.querySelector('[data-prov="verified"]')!;
    expect(chip).toHaveTextContent("Verified");
    expect(chip.className).toContain("border-double");
    expect(chip.querySelector("[data-prov-badge]")).not.toBeNull();
  });

  it("compact keeps the label for screen readers", () => {
    render(<ProvenanceChip prov="assumption" compact />);
    expect(screen.getByText("Assumption")).toHaveClass("sr-only");
  });
});

describe("AvatarStack", () => {
  it("shows up to max avatars and +N, as one labelled group", () => {
    render(
      <AvatarStack
        agents={[
          { id: "a", name: "Intake" },
          { id: "b", name: "Sourcing analyst", state: "running" },
          { id: "c", name: "Logistics analyst" },
          { id: "d", name: "Critic" },
        ]}
        max={2}
      />,
    );
    const group = screen.getByRole("group", { name: "Agents consulted: Intake, Sourcing analyst, Logistics analyst, Critic" });
    expect(group).toHaveTextContent("+2");
  });

  it("supports people", () => {
    render(<AvatarStack people={[{ id: "p", name: "Priya Shah" }]} />);
    expect(screen.getByRole("group", { name: "People: Priya Shah" })).toHaveTextContent("PS");
  });
});

describe("StageChips", () => {
  it("marks the active step and spells state out", () => {
    render(
      <StageChips
        stages={[
          { id: "preparing_changes", label: "Preparing changes", state: "done" },
          { id: "awaiting_approval", label: "Awaiting approval", state: "active" },
          { id: "applying", state: "pending" },
        ]}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items[1]).toHaveAttribute("aria-current", "step");
    expect(items[0]).toHaveTextContent("Preparing changes (done)");
    expect(items[2]).toHaveTextContent("Applying (not started)");
  });
});

describe("Buttons, chips, cards", () => {
  it("Button shows loading state and stays focusable when disabled", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<Button onClick={onClick}>Apply changes for me</Button>);
    await user.click(screen.getByRole("button", { name: "Apply changes for me" }));
    expect(onClick).toHaveBeenCalledOnce();
    rerender(
      <Button onClick={onClick} loading>
        Apply changes for me
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Apply changes for me" });
    expect(btn).toHaveAttribute("aria-busy", "true");
    await user.click(btn);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("IconButton requires a label", () => {
    render(<IconButton label="Close canvas" icon={<span>x</span>} tooltip={false} />);
    expect(screen.getByRole("button", { name: "Close canvas" })).toBeInTheDocument();
  });

  it("Chip is static text without onClick and a toggle with it", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const { rerender } = render(<Chip>Static</Chip>);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    rerender(
      <Chip onClick={onClick} selected={false}>
        Toggle
      </Chip>,
    );
    await user.click(screen.getByRole("button", { name: "Toggle" }));
    expect(onClick).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Toggle" })).toHaveAttribute("aria-pressed", "false");
  });

  it("Card + header + badge + empty state render accessible structure", async () => {
    const { container } = render(
      <div>
        <Card labelledBy="t1">
          <CardHeader title="Exposure" titleId="t1" description="Preliminary" actions={<Badge tone="warning">Preliminary</Badge>} />
        </Card>
        <EmptyState title="No alerts yet" description="Proactive detections will appear here." />
      </div>,
    );
    expect(screen.getByRole("region", { name: "Exposure" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Exposure" })).toBeInTheDocument();
    // Inside a page, the empty state's title is plain text (no extra heading in the outline).
    expect(screen.queryByRole("heading", { name: "No alerts yet" })).not.toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("EmptyState can be the page heading (not found / no access / error pages)", async () => {
    const { container } = render(<EmptyState title="Not found" description="This page doesn't exist." headingLevel={1} />);
    expect(screen.getByRole("heading", { level: 1, name: "Not found" })).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });
});

describe("MessageBubble", () => {
  it("labels authors and aligns user vs assistant", () => {
    render(
      <>
        <MessageBubble role="user">A trim supplier is 3 weeks late</MessageBubble>
        <MessageBubble role="assistant" author="Keelson">
          Three POs slip into peak.
        </MessageBubble>
      </>,
    );
    expect(screen.getByRole("article", { name: "You said" })).toHaveAttribute("data-role", "user");
    expect(screen.getByRole("article", { name: "Keelson said" })).toHaveTextContent("Three POs slip into peak.");
  });
});

function CanvasHarness({ mode }: { mode: "side" | "sheet" }) {
  const [open, setOpen] = useState(true);
  const [tab, setTab] = useState("result");
  return (
    <CanvasPanel
      mode={mode}
      open={open}
      onOpenChange={setOpen}
      tabs={[
        { id: "result", label: "Result" },
        { id: "evidence", label: "Evidence", badge: 4 },
        { id: "trace", label: "Trace" },
      ]}
      activeTab={tab}
      onTabChange={setTab}
    >
      <p>Content for {tab}</p>
    </CanvasPanel>
  );
}

describe("CanvasPanel", () => {
  it("side mode: complementary region with tabs and close", async () => {
    const user = userEvent.setup();
    render(<CanvasHarness mode="side" />);
    expect(screen.getByRole("complementary", { name: "Canvas" })).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "Canvas sections" })).toBeInTheDocument();
    expect(screen.getByText("Content for result")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Evidence/ }));
    expect(screen.getByText("Content for evidence")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Evidence/ })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("button", { name: "Close canvas" }));
    await waitFor(() => expect(screen.queryByRole("complementary")).not.toBeInTheDocument());
  });

  it("auto mode picks a modal sheet below 1280px", async () => {
    setMatchMedia(() => false);
    render(
      <CanvasPanel open onOpenChange={() => {}} tabs={[{ id: "result", label: "Result" }]} activeTab="result" onTabChange={() => {}}>
        <p>Sheet content</p>
      </CanvasPanel>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Canvas" });
    expect(dialog).toHaveTextContent("Sheet content");
  });

  it("auto mode is a side panel at ≥1280px", () => {
    setMatchMedia((q) => q.includes("min-width: 1280px"));
    render(
      <CanvasPanel open onOpenChange={() => {}} tabs={[{ id: "result", label: "Result" }]} activeTab="result" onTabChange={() => {}}>
        <p>Side content</p>
      </CanvasPanel>,
    );
    expect(screen.getByRole("complementary", { name: "Canvas" })).toHaveTextContent("Side content");
  });

  it("has no axe violations (side)", async () => {
    const { container } = render(<CanvasHarness mode="side" />);
    await expectNoA11yViolations(container);
  });
});

function FocusHarness({ mode, dropOpener = false }: { mode: "side" | "sheet"; dropOpener?: boolean }) {
  const [open, setOpen] = useState(false);
  const toggle = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button ref={toggle} type="button">
        Canvas toggle
      </button>
      {dropOpener && open ? null : (
        <button type="button" onClick={() => setOpen(true)}>
          View all changes
        </button>
      )}
      <button type="button" onClick={() => setOpen(false)}>
        Close from outside
      </button>
      <CanvasPanel
        mode={mode}
        open={open}
        onOpenChange={setOpen}
        tabs={[{ id: "result", label: "Result" }]}
        activeTab="result"
        onTabChange={() => {}}
        returnFocusRef={toggle}
      >
        <button type="button">Inside</button>
      </CanvasPanel>
    </>
  );
}

describe("CanvasPanel focus return (regression)", () => {
  it("side: closing from inside returns focus to the element that opened it", async () => {
    const user = userEvent.setup();
    render(<FocusHarness mode="side" />);
    await user.click(screen.getByRole("button", { name: "View all changes" }));
    const panel = screen.getByRole("complementary", { name: "Canvas" });
    await user.click(within(panel).getByRole("button", { name: "Inside" }));
    await user.click(within(panel).getByRole("button", { name: "Close canvas" }));
    expect(screen.getByRole("button", { name: "View all changes" })).toHaveFocus();
  });

  it("side: falls back to returnFocusRef when the opener is gone", async () => {
    const user = userEvent.setup();
    render(<FocusHarness mode="side" dropOpener />);
    await user.click(screen.getByRole("button", { name: "View all changes" }));
    const panel = screen.getByRole("complementary", { name: "Canvas" });
    await user.click(within(panel).getByRole("button", { name: "Close canvas" }));
    expect(screen.getByRole("button", { name: "Canvas toggle" })).toHaveFocus();
  });

  it("side: a close triggered from outside leaves focus where it is", async () => {
    const user = userEvent.setup();
    render(<FocusHarness mode="side" />);
    await user.click(screen.getByRole("button", { name: "View all changes" }));
    await user.click(screen.getByRole("button", { name: "Close from outside" }));
    expect(screen.getByRole("button", { name: "Close from outside" })).toHaveFocus();
  });

  it("sheet: Escape returns focus to the opener", async () => {
    const user = userEvent.setup();
    render(<FocusHarness mode="sheet" />);
    await user.click(screen.getByRole("button", { name: "View all changes" }));
    await screen.findByRole("dialog", { name: "Canvas" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "View all changes" })).toHaveFocus());
  });

  it("sheet: falls back to returnFocusRef when the opener is gone", async () => {
    const user = userEvent.setup();
    render(<FocusHarness mode="sheet" dropOpener />);
    await user.click(screen.getByRole("button", { name: "View all changes" }));
    await screen.findByRole("dialog", { name: "Canvas" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "Canvas toggle" })).toHaveFocus());
  });
});

describe("Tabs + Dialog + Toast", () => {
  it("Tabs supports keyboard navigation", async () => {
    const user = userEvent.setup();
    function T() {
      const [v, setV] = useState("a");
      return (
        <Tabs tabs={[{ id: "a", label: "A" }, { id: "b", label: "B" }]} value={v} onValueChange={setV} label="Sections">
          {(active) => <p>Panel {active}</p>}
        </Tabs>
      );
    }
    render(<T />);
    await user.click(screen.getByRole("tab", { name: "A" }));
    await user.keyboard("{ArrowRight}{Enter}");
    expect(screen.getByText("Panel b")).toBeInTheDocument();
  });

  it("Dialog is labelled by its title and closes", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <Dialog open onOpenChange={onOpenChange} title="Edit approach" description="Tell me what to change.">
        <p>Body</p>
      </Dialog>,
    );
    expect(await screen.findByRole("dialog", { name: "Edit approach" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("toast() queues into the Toaster", async () => {
    render(
      <UiProvider toaster={false}>
        <Toaster />
      </UiProvider>,
    );
    act(() => {
      toast.success("Changes applied", { description: "12 lines in Merxa" });
    });
    expect(await screen.findByText("Changes applied")).toBeInTheDocument();
    expect(screen.getByText("12 lines in Merxa")).toBeInTheDocument();
  });

  // Browser e2e finding: the "Merxa connected" toast, raised in an effect while the case page
  // mounts, was lost — the Toaster (a later sibling) had not subscribed yet.
  it("keeps toasts raised while the page mounts, before the Toaster subscribes", async () => {
    function AnnounceOnMount() {
      useEffect(() => {
        toast.success("Merxa connected", { description: "You can approve changes now." });
        const gone = toast("Dropped before the Toaster mounted");
        toast.dismiss(gone);
      }, []);
      return <p>Case</p>;
    }
    render(
      <UiProvider>
        <AnnounceOnMount />
      </UiProvider>,
    );
    expect(await screen.findByText("Merxa connected")).toBeInTheDocument();
    expect(screen.getByText("You can approve changes now.")).toBeInTheDocument();
    expect(screen.queryByText("Dropped before the Toaster mounted")).not.toBeInTheDocument();
  });

  // Browser e2e finding: bottom-right toasts covered the composer's Send button at 1280 px.
  it("shows toasts at the top, clear of the composer", async () => {
    render(<UiProvider>{null}</UiProvider>);
    act(() => {
      toast("Saved");
    });
    const title = await screen.findByText("Saved");
    const viewport = title.closest("[data-placement]");
    expect(viewport).toHaveAttribute("data-placement", "top");
    expect(viewport).toHaveClass("top-16");
    expect(viewport?.className).not.toMatch(/(^|\s)bottom-/);
  });

  it("createBufferedToastManager replays queued calls in order once subscribed", () => {
    const manager = createBufferedToastManager();
    const id = manager.add({ title: "A" });
    manager.update(id, { title: "A2" });
    manager.add({ title: "B", id: "b" });
    const events: string[] = [];
    const off = manager[" subscribe"]((e) => events.push(`${e.action}:${e.options.id}`));
    expect(events).toEqual([`add:${id}`, `update:${id}`, "add:b"]);
    manager.close("b");
    expect(events.at(-1)).toBe("close:b");
    off();
  });
});

describe("helpers", () => {
  it("formatElapsed", () => {
    expect(formatElapsed(0)).toBe("0s");
    expect(formatElapsed(12_400)).toBe("12s");
    expect(formatElapsed(65_000)).toBe("1m 05s");
    expect(formatElapsed(3_720_000)).toBe("1h 02m");
  });
  it("initials", () => {
    expect(initials("Inventory & demand analyst")).toBe("ID");
    expect(initials("Critic")).toBe("CR");
    expect(initials("")).toBe("?");
  });
});
