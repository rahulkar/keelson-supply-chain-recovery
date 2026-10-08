import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { keelsonApi } from "./support/api";
import { KEELSON } from "./support/env";
import { expect, test } from "./support/fixtures";
import { latestCase, startCase, waitForCaseStatus } from "./support/flows";
import { block, canvas, canvasVisuals, expandAllThinking, signInAs } from "./support/keelson";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function expectNoSeriousViolations(page: Page, where: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({ id: v.id, impact: v.impact, help: v.help, targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) }));
  await test.info().attach(`axe-${where}.json`, {
    body: JSON.stringify(results.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length })), null, 2),
    contentType: "application/json",
  });
  expect(serious, `${where}: serious/critical axe violations`).toEqual([]);
}

/** A finished case to scan: the newest committed one (golden journey), else a quick question. */
async function caseToScan(): Promise<string> {
  const committed = (await latestCase("priya", "committed")) ?? (await latestCase("marco", "committed"));
  if (committed) return committed;
  const api = await keelsonApi("priya");
  try {
    const id = await startCase(api, "Hello! What can you help me with?", "E2E a11y");
    await waitForCaseStatus("priya", id, ["open", "closed", "committed"]);
    return id;
  } finally {
    await api.ctx.dispose();
  }
}

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test.describe(`axe (${viewport.name})`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test(`Home has no serious or critical violations (${viewport.name})`, async ({ page }) => {
      await signInAs(page, "priya");
      await page.goto(`${KEELSON}/`);
      await expect(page.getByRole("heading", { level: 1, name: /Hello/ })).toBeVisible();
      await expectNoSeriousViolations(page, `home-${viewport.name}`);
      await page.getByRole("button", { name: /^Demo persona/ }).click();
      await expect(page.getByRole("menuitem").first()).toBeVisible();
      await expectNoSeriousViolations(page, `home-persona-menu-${viewport.name}`);
    });

    test(`case page has no serious or critical violations (${viewport.name})`, async ({ page }) => {
      test.setTimeout(5 * 60_000);
      const caseId = await caseToScan();
      await signInAs(page, "priya");
      await page.goto(`${KEELSON}/cases/${caseId}`);
      await expect(page.getByRole("region", { name: /^Thinking, turn 1$/ })).toBeVisible({ timeout: 30_000 });
      await expectNoSeriousViolations(page, `case-${viewport.name}`);

      // Everything expanded: every timeline and every agent row.
      await expandAllThinking(page);
      await expectNoSeriousViolations(page, `case-expanded-${viewport.name}`);

      const toggle = page.getByRole("button", { name: "Open canvas" });
      if (await toggle.isVisible()) {
        await toggle.click();
        await expect(canvas(page)).toBeVisible();
        // M3 visuals (comparison, map, graph, timeline) are lazy: scan them once drawn.
        if ((await block(canvas(page), "scenario_compare").count()) > 0) await canvasVisuals(page, canvas(page));
        await expectNoSeriousViolations(page, `case-canvas-${viewport.name}`);
      }
    });
  });
}
