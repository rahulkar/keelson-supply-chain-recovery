/**
 * Act 2 "what-if" (docs/contracts/90 §8) in a real browser against the real stack: Priya asks
 * "What if the Suez Canal closes for 3 weeks?" → the approach card carries the What-if badge →
 * "Looks right" → options are compared, but Apply is disabled ("promote it to an actual case
 * first") and Promote is offered → Promote → the case becomes actual, the options are
 * re-simulated and Apply is enabled. Nothing is applied here (the golden journey covers commit).
 */
import AxeBuilder from "@axe-core/playwright";
import { resetMerxa } from "./support/api";
import { KEELSON, RUN_TIMEOUT_MS } from "./support/env";
import { expect, keyScreenshot, test } from "./support/fixtures";
import { block, caseIdFromUrl, expectCalmTurns, signInAs, turns, waitForBlock } from "./support/keelson";

const SUEZ_PROMPT = "What if the Suez Canal closes for 3 weeks?";
const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

test.describe.configure({ mode: "serial" });

test("act2-whatif", async ({ page }) => {
  test.setTimeout(12 * 60_000);

  await test.step("demo reset: Merxa golden seed (Suez lanes) + Keelson resync", async () => {
    await resetMerxa();
  });

  await test.step("Priya asks the what-if on Home", async () => {
    await signInAs(page, "priya");
    await page.goto(`${KEELSON}/`);
    const box = page.getByRole("textbox", { name: "Describe a disruption or ask a question" });
    await box.fill(SUEZ_PROMPT);
    await box.press("Enter");
    await page.waitForURL(/\/cases\/[^/]+$/, { timeout: 30_000 });
    test.info().annotations.push({ type: "case", description: caseIdFromUrl(page) });
  });

  await test.step("approach card: What-if badge and the planned checks → Looks right", async () => {
    const approach = block(turns(page), "approach_card").first();
    await expect(approach).toBeVisible({ timeout: RUN_TIMEOUT_MS });
    await expect(approach.getByText("What-if", { exact: true })).toBeVisible();
    await expect(approach.locator("[data-what-if]")).toContainText("nothing can be applied until you promote it");
    await expect(approach.getByRole("region", { name: "What I'll check" }).getByRole("listitem").first()).toBeVisible();
    // A what-if card does not offer "Treat as what-if" again.
    await expect(approach.getByRole("button", { name: "Treat as what-if" })).toHaveCount(0);
    const confirm = approach.getByRole("button", { name: "Looks right" });
    if (await confirm.isVisible()) {
      await expect(confirm).toBeEnabled({ timeout: 30_000 });
      await confirm.click();
    } else {
      test.info().annotations.push({ type: "note", description: "approach auto-confirmed by the engine" });
    }
  });

  await test.step("results: Apply disabled with the promote reason, Promote offered", async () => {
    const option = await waitForBlock(page, "recommended_option");
    const resultTurn = turns(page).filter({ has: page.locator('[data-block-kind="recommended_option"]') }).last();
    await expect(resultTurn.locator("section[data-status]").first()).toHaveAttribute("data-status", "done", {
      timeout: RUN_TIMEOUT_MS,
    });
    await expect(page.getByText("What-if").first()).toBeVisible();
    const apply = option.getByRole("button", { name: "Apply changes for me" });
    await expect(apply).toBeDisabled();
    await expect(option).toContainText(/promote it to an actual case first/i);
    const promote = option.getByRole("button", { name: /^Promote to actual/ });
    await expect(promote).toBeEnabled();
    expect(await resultTurn.innerText()).not.toMatch(/\{\{|\}\}/);
    await expectCalmTurns(page);
    await option.scrollIntoViewIfNeeded();
    await keyScreenshot(page, "19-whatif-promote");
    const axe = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
    const serious = axe.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id);
    expect(serious, "what-if result: serious/critical axe violations").toEqual([]);
  });

  await test.step("Promote → actual case, options re-simulated, Apply enabled", async () => {
    const count = await block(turns(page), "recommended_option").count();
    await block(turns(page), "recommended_option").last().getByRole("button", { name: /^Promote to actual/ }).click();
    await expect(block(turns(page), "recommended_option")).toHaveCount(count + 1, { timeout: RUN_TIMEOUT_MS });
    const option = block(turns(page), "recommended_option").last();
    const turn = turns(page).last();
    await expect(turn.locator("section[data-status]").first()).toHaveAttribute("data-status", "done", { timeout: RUN_TIMEOUT_MS });
    await expect(option.getByRole("button", { name: /^Promote to actual/ })).toHaveCount(0);
    const confirm = option.getByRole("button", { name: "Confirm these values" });
    if (await confirm.isVisible()) {
      const n = await block(turns(page), "recommended_option").count();
      await confirm.click();
      await expect(block(turns(page), "recommended_option")).toHaveCount(n + 1, { timeout: RUN_TIMEOUT_MS });
    }
    await expect(block(turns(page), "recommended_option").last().getByRole("button", { name: "Apply changes for me" })).toBeEnabled({
      timeout: 30_000,
    });
    expect(await turn.innerText()).not.toMatch(/\{\{|\}\}/);
  });
});
