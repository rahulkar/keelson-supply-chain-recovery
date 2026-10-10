import { KEELSON } from "./support/env";
import { expect, test } from "./support/fixtures";
import { signInAs } from "./support/keelson";

/** `@keelson/viz/fixtures` (packages/viz/src/fixtures.ts), in order (M1 blocks, the M3 visuals, then M4). */
const FIXTURES: { name: string; kind: string | null }[] = [
  { name: "narrative", kind: "narrative" },
  { name: "kpi_strip", kind: "kpi_strip" },
  { name: "kpi_strip.final", kind: "kpi_strip" },
  { name: "approach_card", kind: "approach_card" },
  { name: "recommended_option", kind: "recommended_option" },
  { name: "plan_diff", kind: "plan_diff" },
  { name: "plan_diff.large", kind: "plan_diff" },
  { name: "approval_card", kind: "approval_card" },
  { name: "approval_card.connected", kind: "approval_card" },
  { name: "commit_tracker.applying", kind: "commit_tracker" },
  { name: "commit_tracker", kind: "commit_tracker" },
  { name: "evidence_pack", kind: "evidence_pack" },
  { name: "invalid.kpi_strip", kind: null },
  { name: "unknown.network_map", kind: null },
  // M3 canvas visuals (lazy, data from the fixture payload)
  { name: "scenario_compare", kind: "scenario_compare" },
  { name: "network_map", kind: "network_map" },
  { name: "impact_graph", kind: "impact_graph" },
  { name: "stockout_timeline", kind: "stockout_timeline" },
  { name: "network_map.empty", kind: "network_map" },
  { name: "network_map.large", kind: "network_map" },
  { name: "invalid.impact_graph.data", kind: null },
  // M4: the approach card with plan checks and agents, a what-if recommendation, a "What changed" line
  { name: "approach_card.m4", kind: "approach_card" },
  { name: "recommended_option.what_if", kind: "recommended_option" },
  { name: "narrative.what_changed", kind: "narrative" },
  // M5: quorum / rejected / bundle approvals, mixed apply + guide, Guide checklists, Undo, re-plan banner
  { name: "approval_card.four_eyes", kind: "approval_card" },
  { name: "approval_card.rejected", kind: "approval_card" },
  { name: "approval_card.bundle", kind: "approval_card" },
  { name: "recommended_option.mixed", kind: "recommended_option" },
  { name: "remediation_checklist", kind: "remediation_checklist" },
  { name: "remediation_checklist.completed", kind: "remediation_checklist" },
  { name: "commit_tracker.undo", kind: "commit_tracker" },
  { name: "commit_tracker.conflicts", kind: "commit_tracker" },
  { name: "commit_tracker.rolled_back", kind: "commit_tracker" },
  { name: "narrative.replan", kind: "narrative" },
  // M6: opened from an alert
  { name: "alert_origin", kind: "alert_origin" },
  { name: "alert_origin.feed", kind: "alert_origin" },
  // M7: plan blocks built on a crawled (tier-A) snapshot carry the Advisory label
  { name: "recommended_option.advisory", kind: "recommended_option" },
  { name: "narrative.advisory", kind: "narrative" },
  { name: "scenario_compare.advisory", kind: "scenario_compare" },
  // M8 (docs/contracts/125): CP-SAT plans, how each option's whole units were found
  { name: "scenario_compare.integer", kind: "scenario_compare" },
  // M8 (docs/contracts/126): likely ranges, extra CO₂e, weight-profile ranks and win rates
  { name: "scenario_compare.robust", kind: "scenario_compare" },
  { name: "kpi_strip.robust", kind: "kpi_strip" },
];

test("/dev/blocks renders every block fixture", async ({ page }) => {
  test.setTimeout(3 * 60_000);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await signInAs(page, "priya");
  await page.goto(`${KEELSON}/dev/blocks`);
  await expect(page.getByRole("heading", { level: 1, name: "Render blocks" })).toBeVisible();

  const sections = page.locator("main section[aria-labelledby^='fx-']");
  await expect(sections).toHaveCount(FIXTURES.length);
  for (const [i, f] of FIXTURES.entries()) {
    const section = sections.nth(i);
    await expect(section.getByRole("heading", { level: 2 })).toContainText(f.name);
    if (f.kind) {
      await expect(section.locator(`[data-block-kind="${f.kind}"]`).first(), f.name).toBeVisible();
      await expect(section.locator("[data-block-fallback]"), `${f.name} must not fall back`).toHaveCount(0);
    } else {
      await expect(section.locator("[data-block-fallback]"), `${f.name} renders as RawDataCard`).toBeVisible();
    }
  }

  // Canvas density re-renders the same set; actions are only previewed.
  await page.getByRole("button", { name: "Canvas" }).click();
  await expect(sections).toHaveCount(FIXTURES.length);
  await sections.nth(3).getByRole("button", { name: "Looks right" }).click();
  await expect(page.getByText(/Not sent: this page only previews blocks/)).toBeVisible();
  expect(errors, "no console errors").toEqual([]);
});
