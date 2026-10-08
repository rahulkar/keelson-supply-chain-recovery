import type { BlockEnvelope } from "@keelson/contracts";
import { act, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BarChart, BlockRenderer, decidedText, decisionLabel, formatDeciders, splitCodes, type BlockAction } from "../src";
import { getFixture } from "../src/fixtures";
import { expectNoA11yViolations } from "./a11y";

const fixture = (name: string): BlockEnvelope => structuredClone(getFixture(name)!.envelope);

describe("record codes never break inside (70 §6 carry-over)", () => {
  it("splitCodes finds hyphenated upper-case codes and leaves prose alone", () => {
    expect(splitCodes("Move PO-BD-0006 and TRIM-BD-01 via DE1-NL1, cut-and-sew 2026-10-05")).toEqual([
      { text: "Move ", code: false },
      { text: "PO-BD-0006", code: true },
      { text: " and ", code: false },
      { text: "TRIM-BD-01", code: true },
      { text: " via ", code: false },
      { text: "DE1-NL1", code: true },
      { text: ", cut-and-sew ", code: false },
      { text: "2026-10-05", code: true },
    ]);
    expect(splitCodes("no codes here")).toEqual([{ text: "no codes here", code: false }]);
  });

  it("approach card wraps every code in a nowrap span (description, values, checks, matched records)", () => {
    const env = fixture("approach_card");
    const props = env.props as Record<string, unknown> & { assumptions: { value_text: string }[]; checks: string[] };
    props.assumptions[0]!.value_text = "Delay on PO-BD-0006 is 21 days";
    props.checks[0] = "Which lines of PO-BD-0006 ship from F-BD-02";
    const { container } = render(<BlockRenderer envelope={env} onAction={() => {}} />);
    const codes = Array.from(container.querySelectorAll("[data-code]")).map((n) => n.textContent);
    expect(codes).toEqual(expect.arrayContaining(["TRIM-BD-01", "F-BD-01", "PO-BD-0006", "F-BD-02"]));
    for (const n of container.querySelectorAll("[data-code]")) expect(n.className).toContain("whitespace-nowrap");
    // matched record codes (mono) are nowrap too
    const mono = Array.from(container.querySelectorAll(".font-mono")).find((n) => n.textContent === "PO-BD-0001");
    expect(mono?.className).toContain("whitespace-nowrap");
  });
});

describe("decided interrupt cards (70 §6 carry-over)", () => {
  it("formats deciders and the note", () => {
    expect(formatDeciders(["Marco Rossi"])).toBe("Marco Rossi");
    expect(formatDeciders(["Ana", "Marco"])).toBe("Ana and Marco");
    expect(formatDeciders(["Ana", "Marco", "Priya", "Sam"])).toBe("Ana, Marco and 2 others");
    expect(formatDeciders([])).toBeNull();
    expect(decidedText({ decision: "approve", by: "Marco Rossi", at: "2026-10-06T14:02:00Z" })).toBe(
      "Decided by Marco Rossi · 6 Oct 2026, 14:02 UTC",
    );
    expect(decidedText({})).toBe("Decided");
    expect(decidedText({ status: "closed" })).toBe("No longer waiting for a decision");
    expect(decidedText({ status: "recorded", decision: "approve", by: "Ana", at: "2026-10-06T14:02:00Z" })).toBe(
      "Recorded for Ana · waiting for the other approvers · 6 Oct 2026, 14:02 UTC",
    );
  });

  it("approach card shows 'Decided by … · time' instead of buttons", async () => {
    const onAction = vi.fn<(a: BlockAction) => void>();
    const { container } = render(
      <BlockRenderer
        envelope={fixture("approach_card")}
        onAction={onAction}
        decision={{ decision: "confirm", by: ["Priya Shah"], at: "2026-10-06T09:15:00Z" }}
      />,
    );
    expect(screen.queryByRole("group", { name: "Approach decision" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Looks right" })).toBeNull();
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
    expect(screen.getByText("Decided by Priya Shah · 6 Oct 2026, 09:15 UTC")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("approval card shows the note and no Connect / Approve call to action", async () => {
    const { container } = render(
      <BlockRenderer
        envelope={fixture("approval_card")}
        onAction={() => {}}
        decision={{ decision: "approve", by: ["Marco Rossi"], at: "2026-10-06T14:02:00Z" }}
      />,
    );
    expect(screen.queryByRole("group", { name: "Approval decision" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Connect Merxa/ })).toBeNull();
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.getByText(/Decided by Marco Rossi/)).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("decisionLabel: known, humanised, and Object.prototype keys stay text", () => {
    expect(decisionLabel("approve")).toBe("Approved");
    expect(decisionLabel("send_back")).toBe("Send back");
    expect(decisionLabel("constructor")).toBe("Constructor");
    expect(decisionLabel("toString")).toBe("ToString");
    expect(decisionLabel(null)).toBeNull();
  });

  it("without a decision the buttons are unchanged", () => {
    render(<BlockRenderer envelope={fixture("approval_card.connected")} onAction={() => {}} />);
    expect(within(screen.getByRole("group", { name: "Approval decision" })).getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });
});

describe("BarChart", () => {
  it("renders an accessible data table alongside the (decorative) chart", async () => {
    const { container } = render(
      <BarChart
        caption="Spend per day, last 7 days"
        unit="eur_cents"
        formatOptions={{ exactCents: true }}
        data={[
          { label: "30 Sep", value: 42 },
          { label: "1 Oct", value: 1250 },
        ]}
      />,
    );
    await act(async () => {});
    const table = screen.getByRole("table", { name: "Spend per day, last 7 days" });
    expect(within(table).getByText("€0.42")).toBeInTheDocument();
    expect(within(table).getByText("€12.50")).toBeInTheDocument();
    expect(container.querySelector("[data-chart=bar]")).toHaveAttribute("aria-hidden", "true");
    await expectNoA11yViolations(container);
  });

  it("follows a theme switch on <html data-theme> (colours are re-applied, not frozen at mount)", async () => {
    const root = document.documentElement;
    const before = root.dataset.theme;
    root.dataset.theme = "light";
    try {
      const { container } = render(<BarChart caption="Spend" unit="eur_cents" data={[{ label: "1 Oct", value: 100 }]} />);
      const host = container.querySelector("[data-chart=bar]")!;
      expect(host).toHaveAttribute("data-chart-theme", "light");
      await act(async () => {
        root.dataset.theme = "dark";
        // MutationObserver callbacks run as microtasks.
        await Promise.resolve();
      });
      expect(host).toHaveAttribute("data-chart-theme", "dark");
    } finally {
      if (before === undefined) delete root.dataset.theme;
      else root.dataset.theme = before;
    }
  });
});
