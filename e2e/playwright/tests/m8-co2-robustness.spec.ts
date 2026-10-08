import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { KEELSON } from "./support/env";
import { elementScreenshot, expect, test } from "./support/fixtures";
import { signInAs } from "./support/keelson";
import { restoreSpec, spec } from "./support/m7";

/**
 * M8 CO₂e and robustness in the browser without a model (docs/contracts/126-m8-co2-robustness.md):
 * the robust `scenario_compare@1` fixture on /dev/blocks (likely ranges, the cost ⇄ CO₂e axis
 * switch, Rank by profiles, "How sure are we?" as chart and as table), the KPI strip with likely
 * ranges and the CO₂e tile, and the admin Policies page's **Option scoring** section (save a new
 * default profile + samples as sam, then restore). Each view is axe-clean. Screenshots 41–43.
 *
 * State: the spec restores the demo-retail agent spec version it found.
 */

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function expectAxeClean(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({ id: v.id, impact: v.impact, help: v.help, targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) }));
  expect(serious, `${where}: serious/critical axe violations`).toEqual([]);
}

function fixtureSection(page: Page, name: string) {
  return page
    .locator("main section[aria-labelledby^='fx-']")
    .filter({ has: page.getByRole("heading", { level: 2, name: new RegExp(`^${name.replace(/\./g, "\\.")}`) }) });
}

test.describe("M8 CO₂e and robustness (no model)", () => {
  test("robust compare: likely ranges, CO₂e axis, profiles, How sure are we?", async ({ page }) => {
    await signInAs(page, "priya");
    await page.goto(`${KEELSON}/dev/blocks`);
    const compare = fixtureSection(page, "scenario_compare.robust");
    await expect(compare).toHaveCount(1);
    await compare.scrollIntoViewIfNeeded();
    const table = compare.getByRole("table", { name: "Scorecard by option" });
    await expect(table.getByText(/likely 91\.26%–97\.16%/)).toBeVisible();
    await expect(table.getByRole("columnheader", { name: "Extra CO₂e" })).toBeVisible();
    await expect(compare.getByText("Top under Net benefit")).toBeVisible();
    await elementScreenshot(compare, "41-robust-compare");

    const axis = compare.getByRole("group", { name: "Compare against" });
    await axis.getByRole("button", { name: "Extra CO₂e" }).click();
    await expect(axis.getByRole("button", { name: "Extra CO₂e" })).toHaveAttribute("aria-pressed", "true");
    await expect(compare.getByText(/compared on extra co₂e and lost units/i)).toBeVisible();

    const panel = compare.getByRole("region", { name: "How sure are we?" });
    await panel.getByRole("button", { name: "View as table" }).click();
    const ranges = panel.getByRole("table", { name: "Likely range of lost margin" });
    await expect(ranges.getByRole("columnheader", { name: "Best in % of futures" })).toBeVisible();
    await expect(ranges.getByRole("row", { name: /Rebalance \+ targeted sea-air/ })).toContainText("68.75%");
    await expectAxeClean(page, "robust-compare");
    await elementScreenshot(compare, "42-robust-table");

    const strip = fixtureSection(page, "kpi_strip.robust");
    await expect(strip.getByText("22,932 kg CO₂e")).toBeVisible();
    await expect(strip.getByText(/Likely 11,974 units–32,358 units/)).toBeVisible();
  });

  test("admin Option scoring: save a default profile and samples, then restore", async ({ page }) => {
    test.setTimeout(2 * 60_000);
    const original = (await spec("sam")).version;
    try {
      await signInAs(page, "sam");
      await page.goto(`${KEELSON}/admin/policies`);
      const section = page.getByRole("region", { name: "Option scoring" });
      await expect(section).toBeVisible();
      await section.scrollIntoViewIfNeeded();
      await expect(section.getByRole("list", { name: "Weight profiles" }).getByRole("listitem")).toHaveCount(4);
      await expect(section.getByText(/glec-v3-indicative \(platform data, not editable\)/)).toBeVisible();
      await expectAxeClean(page, "option-scoring");
      await elementScreenshot(section, "43-scoring-admin");

      await section.getByLabel("Default profile").selectOption("cost-first");
      await section.getByLabel("Sampled futures per option").fill("8");
      await expect(section.getByText("A whole number from 16 to 256.")).toBeVisible();
      await expect(section.getByRole("button", { name: "Save scoring" })).toBeDisabled();
      await section.getByLabel("Sampled futures per option").fill("96");
      await section.getByRole("button", { name: "Save scoring" }).click();
      await expect(page.getByText(/Option scoring saved/)).toBeVisible();
      const after = await spec("sam");
      const planning = after.spec["planning"] as { scoring: { default_profile: string }; robustness: { samples: number } };
      expect(after.version).not.toBe(original);
      expect(planning.scoring.default_profile).toBe("cost-first");
      expect(planning.robustness.samples).toBe(96);
    } finally {
      await restoreSpec(original);
    }
  });
});
