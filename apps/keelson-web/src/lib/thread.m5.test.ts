import type { BlockEnvelope } from "@keelson/contracts";
import { getFixture } from "@keelson/viz/fixtures";
import { describe, expect, it } from "vitest";
import { isUpdatedSince, markCaseSeen, caseSeenAt } from "./seen";
import { initialCaseStreamState, type BlockEntry, type CaseStreamState } from "./stream/reducer";
import { bannerOf, latestBanner, planVersionViews } from "./thread";

const fx = (name: string): BlockEnvelope => structuredClone(getFixture(name)!.envelope);

function stateOf(envelopes: BlockEnvelope[]): CaseStreamState {
  const blocks: Record<string, BlockEntry> = {};
  envelopes.forEach((envelope, i) => {
    blocks[envelope.id] = { envelope, runId: "run-1", seq: i + 1, revision: 1, live: false };
  });
  return { ...initialCaseStreamState, blocks, blockOrder: envelopes.map((e) => e.id) };
}

function planDiff(id: string, version: number, rows: { target: string; after: string }[]): BlockEnvelope {
  const base = fx("plan_diff");
  return {
    ...base,
    id,
    props: {
      plan_version: version,
      rows: rows.map((r, i) => ({ seq: i + 1, op: "transfer_order.create", target_label: r.target, before_text: "No transfer", after_text: r.after, qty: 100, system: "Merxa" })),
      totals: { lines: rows.length, units: rows.length * 100, value_cents: 0 },
    },
  };
}

describe("re-plan banner (M5)", () => {
  it("bannerOf / latestBanner read the newest valid narrative banner", () => {
    const replan = fx("narrative.replan");
    const s = stateOf([fx("narrative"), replan, fx("plan_diff")]);
    expect(latestBanner(s)?.text).toBe("New information: sea-air capacity on CGP-UK1-SA cut to 30% — plan updated (v2)");
    expect(bannerOf([])).toBeNull();
    // An invalid narrative never yields a banner.
    const broken = { ...replan, id: "blk_broken", props: { ...(replan.props as object), markdown: "" } };
    expect(latestBanner(stateOf([broken]))).toBeNull();
  });
});

describe("planVersionViews (M5 version switcher)", () => {
  it("uses the narrative's plan_versions, the latest diff per version and the banner's what-changed", () => {
    const v1 = planDiff("blk_pd_v1", 1, [{ target: "A", after: "Ship 600" }]);
    const v1b = planDiff("blk_pd_v1b", 1, [{ target: "A", after: "Ship 650" }]);
    const v2 = planDiff("blk_pd_v2", 2, [{ target: "A", after: "Ship 650" }, { target: "C", after: "Ship 900" }]);
    const views = planVersionViews(stateOf([v1, v1b, fx("narrative.replan"), v2]));
    expect(views.map((v) => [v.version, v.label, v.status, v.current])).toEqual([
      [1, "Plan v1", "committed", false],
      [2, "Plan v2", "proposed", true],
    ]);
    expect(views[0]!.diff?.rows[0]!.after_text).toBe("Ship 650");
    expect(views[1]!.diff?.rows).toHaveLength(2);
    expect(views[1]!.whatChanged).toMatch(/^Plan v2: 4 sea-air bookings → 2/);
    expect(views[0]!.whatChanged).toBeNull();
  });

  it("without plan_versions: every version seen in plan diffs / options, the newest proposed", () => {
    const views = planVersionViews(stateOf([planDiff("a", 1, [{ target: "A", after: "x" }]), fx("recommended_option")]));
    expect(views.map((v) => [v.version, v.status, v.current])).toEqual([
      [1, "superseded", false],
      [2, "proposed", true],
    ]);
    expect(views[1]!.diff).toBeNull();
    expect(planVersionViews(initialCaseStreamState)).toEqual([]);
  });
});

describe("case list 'Updated' badge", () => {
  it("compares updated_at with the viewer's last look", () => {
    expect(isUpdatedSince("2026-10-07T10:00:00Z", "2026-10-07T09:00:00Z")).toBe(true);
    expect(isUpdatedSince("2026-10-07T08:00:00Z", "2026-10-07T09:00:00Z")).toBe(false);
    expect(isUpdatedSince("2026-10-07T10:00:00Z", null)).toBe(false);
    expect(isUpdatedSince(null, "2026-10-07T09:00:00Z")).toBe(false);
    expect(isUpdatedSince("nope", "2026-10-07T09:00:00Z")).toBe(false);
  });

  it("markCaseSeen stores the instant per case", () => {
    markCaseSeen("c-seen", new Date("2026-10-07T09:30:00Z"));
    expect(caseSeenAt("c-seen")).toBe("2026-10-07T09:30:00.000Z");
    expect(caseSeenAt("c-other")).toBeNull();
  });
});
