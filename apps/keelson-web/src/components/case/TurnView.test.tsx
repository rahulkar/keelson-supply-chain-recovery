import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Turn } from "@/lib/thread";
import { TurnView } from "./TurnView";

const turn = (over: Partial<Turn> = {}): Turn => ({
  key: "run:r2",
  runId: "r2",
  user: { id: "m2", role: "user", text: "What if the delay doubles?", run_id: "r2", created_at: "2026-10-07T09:00:00Z" },
  run: null,
  narrative: "- **The gap grows to 9,800 units.**",
  whatChanged: "Plan v2: delay 7 → 14 days; units protected 4,300 → 6,900",
  banner: null,
  inline: [],
  stale: false,
  ...over,
});

describe("TurnView (M4 follow-ups)", () => {
  it("shows the compact What changed line before the narrative, and chips send chat follow-ups", async () => {
    const user = userEvent.setup();
    const onFollowUp = vi.fn();
    const { container } = render(
      <TurnView
        turn={turn()}
        index={1}
        interrupts={{}}
        assistantName="Keelson"
        gate={() => ({ disabled: false })}
        onAction={async () => {}}
        followUps={["Why not Rebalance between DCs?"]}
        onFollowUp={onFollowUp}
        followUpsDisabled={false}
      />,
    );
    const line = container.querySelector("[data-what-changed]");
    expect(line).toHaveTextContent("What changed: Plan v2: delay 7 → 14 days; units protected 4,300 → 6,900");
    // The line comes before the narrative text.
    const narrative = screen.getByText("The gap grows to 9,800 units.");
    expect(line!.compareDocumentPosition(narrative) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Why not Rebalance between DCs?" }));
    expect(onFollowUp).toHaveBeenCalledWith("Why not Rebalance between DCs?");
  });

  it("no line when the turn has no what_changed", () => {
    const { container } = render(
      <TurnView
        turn={turn({ whatChanged: null })}
        index={0}
        interrupts={{}}
        assistantName="Keelson"
        gate={() => ({ disabled: false })}
        onAction={async () => {}}
        followUps={[]}
        onFollowUp={() => {}}
        followUpsDisabled={false}
      />,
    );
    expect(container.querySelector("[data-what-changed]")).toBeNull();
  });
});
