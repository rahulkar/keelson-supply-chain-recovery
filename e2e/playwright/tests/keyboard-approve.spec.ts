/**
 * Keyboard-only approval: Marco reaches and operates Connect Merxa (when needed) and Approve with
 * Tab / Enter only, with a visible focus indicator on each control. The case is prepared through
 * the API (Priya: trim delay → confirm → apply), so only the approval is driven in the browser.
 */
import { resetMerxa } from "./support/api";
import { AUTH, KEELSON, RUN_TIMEOUT_MS, demoPassword } from "./support/env";
import { expect, test } from "./support/fixtures";
import { prepareApprovalCase } from "./support/flows";
import { block, turns, waitForBlock } from "./support/keelson";
import type { Locator, Page } from "@playwright/test";

/** Press Tab until `target` has focus (fails after `max` presses); returns the number of presses. */
async function tabTo(page: Page, target: Locator, max = 120): Promise<number> {
  for (let i = 1; i <= max; i++) {
    await page.keyboard.press("Tab");
    if (await target.evaluate((el) => el === document.activeElement)) return i;
  }
  throw new Error(`could not reach the control with Tab in ${max} presses`);
}

async function expectVisibleFocus(target: Locator): Promise<void> {
  const style = await target.evaluate((el) => {
    const s = getComputedStyle(el);
    return { outline: s.outlineStyle, width: parseFloat(s.outlineWidth), shadow: s.boxShadow, visible: el.matches(":focus-visible") };
  });
  expect(style.visible, "focus-visible matches after keyboard navigation").toBe(true);
  expect(style.outline !== "none" && style.width > 0 || style.shadow !== "none", "visible focus indicator").toBe(true);
}

test("keyboard-only approve flow", async ({ page, tracer }) => {
  test.setTimeout(12 * 60_000);
  await resetMerxa();
  const caseId = await prepareApprovalCase();

  // Marco's browser session (API login-as shares the context's cookie jar).
  const login = await page.request.post(`${KEELSON}/api/demo/login-as`, {
    data: { username: "marco" },
    headers: { Origin: KEELSON },
  });
  expect(login.status()).toBe(200);
  await page.goto(`${KEELSON}/cases/${caseId}`);
  const approval = await waitForBlock(page, "approval_card", 60_000);
  await expect(approval.locator('[data-connect-state="checking"]')).toHaveCount(0, { timeout: 30_000 });

  const connect = approval.getByRole("button", { name: /^(Re)?connect Merxa$/ });
  // The card settles on either "Connect Merxa" or an enabled Approve (the link check is async; the
  // "checking" note may not have rendered yet when the line above ran).
  await expect
    .poll(async () => (await connect.isVisible()) || (await approval.getByRole("button", { name: "Approve" }).isEnabled()), {
      timeout: 30_000,
    })
    .toBe(true);
  if (await connect.isVisible()) {
    await tabTo(page, connect);
    await expectVisibleFocus(connect);
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`^${AUTH.replace(/\./g, "\\.")}/realms/merxa/`), { timeout: 30_000 });
    const username = page.locator("#username");
    await expect(username).toBeVisible();
    // Keyboard only: the username field has focus on load; type, Tab to the password, Enter.
    await expect(username).toBeFocused();
    if ((await username.inputValue()) !== "marco.rossi") {
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.type("marco.rossi");
    }
    await tracer.secret(async () => {
      await page.keyboard.press("Tab");
      await expect(page.locator("#password")).toBeFocused();
      await page.keyboard.type(demoPassword());
      await page.keyboard.press("Enter");
      await page.waitForURL((url) => !url.href.startsWith(AUTH), { timeout: 30_000, waitUntil: "commit" });
    });
    await page.waitForURL(new RegExp(`/cases/${caseId}`), { timeout: 30_000 });
  }

  const card = await waitForBlock(page, "approval_card", 60_000);
  const approve = card.getByRole("button", { name: "Approve" });
  await expect(approve).toBeEnabled({ timeout: 30_000 });
  // Start from the top of the document so the whole tab order is exercised.
  await page.locator("body").evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  const presses = await tabTo(page, approve);
  test.info().annotations.push({ type: "tab-presses-to-approve", description: String(presses) });
  await expectVisibleFocus(approve);
  await page.keyboard.press("Enter");

  const tracker = await waitForBlock(page, "commit_tracker");
  await expect(tracker.locator('[data-status="verified"]')).toBeVisible({ timeout: RUN_TIMEOUT_MS });
  // M2 (70-backoffice-m2.md carry-overs): a decided card shows a "Decided by … · time" note
  // instead of dead buttons.
  const decided = block(turns(page), "approval_card").last();
  await expect(decided.locator('[data-decided="decided"]')).toContainText(/Approved\s*Decided by Marco Rossi/);
  await expect(decided.getByRole("button", { name: "Approve" })).toHaveCount(0);
});
