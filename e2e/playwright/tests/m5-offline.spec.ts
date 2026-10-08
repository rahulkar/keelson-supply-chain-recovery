import AxeBuilder from "@axe-core/playwright";
import type { Locator, Page } from "@playwright/test";
import { keelsonApi } from "./support/api";
import { KEELSON } from "./support/env";
import { elementScreenshot, expect, keyScreenshot, test } from "./support/fixtures";
import { signInAs } from "./support/keelson";

/**
 * M5 browser checks that never run a model (docs/contracts/95 §7): the M5 block fixtures on
 * /dev/blocks (Guide checklist, four-eyes quorum progress, re-plan banner, commit tracker Undo),
 * the admin Policies page (edit + guard + test bench) and the demo reset from the persona menu.
 * Screenshots 22–26 come from these fixture / admin pages (the live Act 3 run replaces 22–25 with
 * real cases once model credits are available).
 */

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function expectAxeClean(page: Page, where: string, include?: string[]): Promise<void> {
  let builder = new AxeBuilder({ page }).withTags(TAGS);
  for (const sel of include ?? []) builder = builder.include(sel);
  const results = await builder.analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({ id: v.id, impact: v.impact, help: v.help, targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) }));
  expect(serious, `${where}: serious/critical axe violations`).toEqual([]);
}

function fixture(page: Page, name: string): Locator {
  return page.locator(`main section[aria-labelledby="fx-${name}"]`);
}

const M5_FIXTURES = [
  "approval_card.four_eyes",
  "approval_card.rejected",
  "approval_card.bundle",
  "recommended_option.mixed",
  "remediation_checklist",
  "remediation_checklist.completed",
  "commit_tracker.undo",
  "commit_tracker.conflicts",
  "commit_tracker.rolled_back",
  "narrative.replan",
];

test("M5 blocks: guide checklist, four-eyes progress, re-plan banner, Undo (fixtures)", async ({ page }) => {
  test.setTimeout(3 * 60_000);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await page.setViewportSize({ width: 1280, height: 1000 });
  await signInAs(page, "priya");
  await page.goto(`${KEELSON}/dev/blocks`);
  await expect(page.getByRole("heading", { level: 1, name: "Render blocks" })).toBeVisible();
  for (const name of M5_FIXTURES) {
    await expect(fixture(page, name).locator("[data-block-kind]").first(), name).toBeVisible();
    await expect(fixture(page, name).locator("[data-block-fallback]"), `${name} must not fall back`).toHaveCount(0);
  }

  // Guide checklist: verified / pending steps, deep links into Merxa in a new tab, manual ticks
  const guide = fixture(page, "remediation_checklist");
  const steps = guide.getByRole("list", { name: "Steps" });
  await expect(steps.locator("[data-step-status='verified']").first()).toBeVisible();
  await expect(steps.locator("[data-step-status='pending']").first()).toBeVisible();
  const open = guide.getByRole("link", { name: /^Open in Merxa/ }).first();
  await expect(open).toHaveAttribute("target", "_blank");
  await expect(open).toHaveAttribute("href", /^http:\/\/merxa\.localhost\//);
  await expect(guide.getByRole("meter", { name: "Guide progress" })).toBeVisible();
  await guide.scrollIntoViewIfNeeded();
  await elementScreenshot(guide, "22-guide-checklist");
  await guide.getByRole("button", { name: "Mark done" }).first().click();
  await expect(page.getByText(/^Mark step \d+ of .+ done$/)).toBeVisible();
  await expect(fixture(page, "remediation_checklist.completed").locator("[data-guide-completed]")).toContainText(
    "Guide completed",
  );

  // Four-eyes: 1 of 2 approvals, waiting for Compliance
  const fourEyes = fixture(page, "approval_card.four_eyes");
  const approvals = fourEyes.getByRole("region", { name: "Approvals" });
  await expect(approvals).toContainText("1 of 2 approvals");
  await expect(approvals).toContainText(/waiting for Compliance/);
  await fourEyes.scrollIntoViewIfNeeded();
  await elementScreenshot(fourEyes, "23-four-eyes");
  await expect(fixture(page, "approval_card.bundle").getByRole("region", { name: "Systems changed" })).toBeVisible();
  await expect(fixture(page, "recommended_option.mixed")).toContainText(/Apply 3 changes for me/);
  await expect(fixture(page, "recommended_option.mixed")).toContainText(/Guide me through 2/);

  // Re-plan banner with the plan versions
  const replan = fixture(page, "narrative.replan");
  const banner = replan.locator("[data-replan-banner]");
  await expect(banner).toContainText("New information");
  await expect(banner).toContainText(/plan updated \(v2\)/);
  await expect(replan.getByRole("list", { name: "Plan versions" })).toBeVisible();
  await replan.scrollIntoViewIfNeeded();
  await elementScreenshot(replan, "24-replan-banner");

  // Commit tracker: Undo changes → confirm → the rollback action (previewed only)
  const undo = fixture(page, "commit_tracker.undo");
  await undo.getByRole("button", { name: "Undo changes" }).click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog")).filter({ hasText: /Undo these changes in/ });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Undo changes" }).click();
  await expect(page.getByText(/^Undo change set /)).toBeVisible();
  await expect(fixture(page, "commit_tracker.conflicts")).toContainText(/conflict/i);
  const rolled = fixture(page, "commit_tracker.rolled_back");
  await expect(rolled).toContainText("Rolled back");
  await rolled.scrollIntoViewIfNeeded();
  await elementScreenshot(rolled, "25-rollback");

  await expectAxeClean(
    page,
    "m5-fixtures",
    M5_FIXTURES.map((n) => `main section[aria-labelledby="fx-${n}"]`),
  );
  expect(errors, "no console errors").toEqual([]);
});

type PolicyRule = { id: string; description: string; when: string; require: unknown; priority: number };
type PoliciesView = { spec_version: string; policy: { version: string; rules: PolicyRule[] } };

async function policies(): Promise<PoliciesView> {
  const api = await keelsonApi("sam");
  try {
    const r = await api.ctx.get("/api/admin/policies");
    expect(r.status()).toBe(200);
    return (await r.json()) as PoliciesView;
  } finally {
    await api.ctx.dispose();
  }
}

/** Put `rules` back (a no-op when they are already active); the demo reset restores the pack spec too. */
async function restorePolicies(rules: PolicyRule[]): Promise<void> {
  const now = await policies();
  if (JSON.stringify(now.policy.rules) === JSON.stringify(rules)) return;
  const api = await keelsonApi("sam");
  try {
    const r = await api.ctx.put("/api/admin/policies", {
      data: { base_version: now.spec_version, rules },
      headers: { "X-CSRF-Token": api.csrf },
    });
    expect([200, 201], `restore policies: ${await r.text()}`).toContain(r.status());
  } finally {
    await api.ctx.dispose();
  }
}

test("admin Policies: rules, guarded edit, saved edit and the test bench (sam)", async ({ page }) => {
  test.setTimeout(3 * 60_000);
  const before = await policies();
  try {
    await page.setViewportSize({ width: 1280, height: 1250 });
    await signInAs(page, "sam");
    await page.goto(`${KEELSON}/admin/policies`);
    await expect(page.getByRole("heading", { level: 1, name: "Policies" })).toBeVisible();
    const list = page.getByRole("list", { name: "Rules in evaluation order" });
    await expect(list.getByRole("listitem")).toHaveCount(before.policy.rules.length);
    await expect(list).toContainText("auto-small-transfer");
    await expectAxeClean(page, "policies");

    // Test bench on the saved rules: the default sample is a small planner transfer → automatic
    await page.getByRole("button", { name: "Test rules" }).click();
    const result = page.locator("[data-test-result]");
    await expect(result).toBeVisible();
    await expect(result).toContainText(/Applied automatically|Needs approval|Refused/);

    // Raising the automatic cap is a guarded change (M7, contract 110 §1): saving it would create a
    // four-eyes change request for a second admin (not saved here)
    await page.getByRole("button", { name: "Edit auto-small-transfer" }).click();
    const cel = page.getByLabel("Condition (CEL)");
    const original = await cel.inputValue();
    await cel.fill(original.replace("cs.units <= 500", "cs.units <= 50000"));
    await expect(page.locator("[data-guarded-summary]")).toContainText("Requires a second admin");
    await expect(page.locator("[data-guarded-summary]")).toContainText("change request");
    await page.getByRole("button", { name: "Discard" }).click();

    // A benign description edit saves as a new policy version
    await page.getByRole("button", { name: "Edit default" }).click();
    const description = page.getByLabel("Description");
    await description.fill(`${await description.inputValue()} (reviewed)`);
    await page.getByRole("button", { name: "Test draft rules" }).click();
    await expect(result).toBeVisible();
    await page.getByRole("button", { name: "Done" }).click();
    await page.getByRole("heading", { level: 1, name: "Policies" }).scrollIntoViewIfNeeded();
    await keyScreenshot(page, "26-policies-admin");
    await page.getByRole("button", { name: "Save rules" }).click();
    await expect(page.getByText(/^Policies saved \(p-\d+\)$/)).toBeVisible();
  } finally {
    await restorePolicies(before.policy.rules);
  }
});

test("demo reset from the persona menu (ava, rehearsal mode)", async ({ page }) => {
  test.setTimeout(3 * 60_000);
  await signInAs(page, "ava");
  await page.goto(`${KEELSON}/`);
  await page.getByRole("button", { name: /^Demo persona: .*Switch persona$/ }).click();
  await page.getByRole("menuitem", { name: /Reset demo/ }).click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog")).filter({ hasText: "Reset the demo?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("checkbox", { name: /Rehearsal: keep Merxa connections/ }).check();
  await expectAxeClean(page, "reset-dialog");
  await dialog.getByRole("button", { name: "Reset demo" }).click();
  const done = page.getByText(/^Demo reset in \d+(\.\d)? (ms|s)$/);
  await expect(done).toBeVisible({ timeout: 90_000 });
  const stepsLog = page.getByRole("list", { name: "Reset steps" });
  await expect(stepsLog.locator("li")).toHaveCount(10); // M6: + signal_hub
  await expect(stepsLog.locator("li[data-state='failed']")).toHaveCount(0);
  await expect(stepsLog.locator("li[data-state='skipped']")).toHaveCount(2); // consents + connections kept
  await expect(page).toHaveURL(`${KEELSON}/`);

  // a planner is not offered the reset (a fresh session: login-as from a signed-in one needs CSRF)
  await page.context().clearCookies();
  await signInAs(page, "priya");
  await page.goto(`${KEELSON}/`);
  await page.getByRole("button", { name: /^Demo persona: .*Switch persona$/ }).click();
  await expect(page.getByRole("menuitem", { name: /Reset demo/ })).toHaveCount(0);
  await page.keyboard.press("Escape");
});
