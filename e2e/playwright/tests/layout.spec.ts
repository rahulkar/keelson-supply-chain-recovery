import { KEELSON } from "./support/env";
import { expect, test } from "./support/fixtures";
import { latestCase } from "./support/flows";
import { canvas, expandAllThinking, expectNoHorizontalOverflow, signInAs } from "./support/keelson";

const VIEWPORTS = [
  { name: "1280", width: 1280, height: 800 },
  { name: "390", width: 390, height: 844 },
];

for (const vp of VIEWPORTS) {
  test.describe(`layout at ${vp.name}px`, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });

    test(`pages fit the viewport (${vp.name}px)`, async ({ page }) => {
      await signInAs(page, "priya");
      for (const path of ["/", "/cases", "/alerts", "/dev/blocks"]) {
        await page.goto(`${KEELSON}${path}`);
        await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
        await expectNoHorizontalOverflow(page, `${path} @${vp.name}`);
      }
      // The header keeps brand, persona switcher and user menu on one row.
      await page.goto(`${KEELSON}/`);
      const header = page.locator("header").first();
      const box = await header.boundingBox();
      expect(box?.height ?? 0).toBeLessThanOrEqual(64);
    });

    test(`case thread and canvas fit the viewport (${vp.name}px)`, async ({ page }) => {
      const caseId = (await latestCase("priya", "committed")) ?? (await latestCase("priya"));
      test.skip(!caseId, "no case yet (run the golden journey first)");
      await signInAs(page, "priya");
      await page.goto(`${KEELSON}/cases/${caseId}`);
      await expect(page.getByRole("region", { name: "Thinking, turn 1", exact: true })).toBeVisible({ timeout: 30_000 });
      await expandAllThinking(page);
      await expectNoHorizontalOverflow(page, `case @${vp.name}`);

      await page.getByRole("button", { name: "Open canvas" }).click();
      const panel = canvas(page);
      await expect(panel).toBeVisible();
      if (vp.width < 1280) await expect(page.getByRole("dialog", { name: "Canvas" })).toBeVisible();
      else await expect(page.getByRole("complementary", { name: "Canvas" })).toBeVisible();
      await expectNoHorizontalOverflow(page, `case+canvas @${vp.name}`);
      await page.keyboard.press("Escape");
      if (vp.width < 1280) {
        await expect(panel).toBeHidden();
        await expect(page.getByRole("button", { name: "Open canvas" })).toBeFocused();
      }
    });
  });
}

test("unknown case shows a calm not-found state", async ({ page }) => {
  await signInAs(page, "priya");
  const res = await page.goto(`${KEELSON}/cases/00000000-0000-7000-8000-000000000000`);
  expect(res?.status()).toBe(404);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: /home|start/i }).first()).toBeVisible();
});
