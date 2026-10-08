import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { keelsonApi } from "./support/api";
import { KEELSON } from "./support/env";
import { elementScreenshot, expect, keyScreenshot, test } from "./support/fixtures";
import { caseIdFromUrl, signInAs } from "./support/keelson";

/**
 * M6 alerts inbox in the browser, without a model (docs/contracts/100 §8): the presenter starts the
 * typhoon pack from the persona menu, the planner sees the Home pill, the inbox row (severity, DEMO
 * badge, why, exposure summary), the detail drawer (exposure list, sources with licence, updates)
 * and opens a case from it. The case's `signal` run must never start, so this spec only runs with
 * the engine worker held (`e2e/playwright/run-m6.sh` starts `e2e/m6_hold.py` around it and sets
 * E2E_M6_WORKER_HELD); the spec cancels the queued run itself before it ends.
 * Screenshots 27–30.
 */

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];
const PACK_TITLE = "Typhoon Kestrel approaching Ho Chi Minh City (simulated)";

async function expectAxeClean(page: Page, where: string, include?: string[]): Promise<void> {
  let builder = new AxeBuilder({ page }).withTags(TAGS);
  for (const sel of include ?? []) builder = builder.include(sel);
  const results = await builder.analyze();
  const serious = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => ({ id: v.id, impact: v.impact, help: v.help, targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) }));
  expect(serious, `${where}: serious/critical axe violations`).toEqual([]);
}

test.describe("M6 alerts (worker held)", () => {
  test.skip(!process.env.E2E_M6_WORKER_HELD, "run through e2e/playwright/run-m6.sh (holds the engine worker)");

  test("presenter pack controls → Home pill → inbox → drawer → open case (queued run)", async ({ page }) => {
    test.setTimeout(4 * 60_000);
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.setViewportSize({ width: 1280, height: 900 });

    // ── presenter (ava): persona menu → Demo scenarios… → Reset, then Start the typhoon pack
    await signInAs(page, "ava");
    await page.goto(`${KEELSON}/`);
    await page.getByRole("button", { name: /^Demo persona: .*Switch persona$/ }).click();
    await page.getByRole("menuitem", { name: /Demo scenarios/ }).click();
    const dialog = page.getByRole("dialog", { name: "Demo scenarios" });
    await expect(dialog).toBeVisible();
    const list = dialog.getByRole("list", { name: "Demo scenarios" });
    await expect(list.getByText(PACK_TITLE)).toBeVisible();
    await expectAxeClean(page, "demo-scenarios-dialog");
    await dialog.getByRole("button", { name: `Reset: ${PACK_TITLE}` }).click();
    await expect(page.getByText(`Reset "${PACK_TITLE}"`)).toBeVisible();
    await dialog.getByRole("button", { name: `Start: ${PACK_TITLE}` }).click();
    await expect(page.getByText(`Started "${PACK_TITLE}"`)).toBeVisible();
    await expect(list.getByText(/Running · step 1 of 6/)).toBeVisible();
    await page.keyboard.press("Escape");

    // ── planner (priya): Home pill (live count from the alert channel)
    await page.context().clearCookies();
    await signInAs(page, "priya");
    await page.goto(`${KEELSON}/`);
    const pill = page.getByRole("link", { name: /new alert/ });
    await expect(pill).toHaveAttribute("data-new-alerts", "1", { timeout: 30_000 });
    await expect(pill).toHaveText("1 new alert");
    await keyScreenshot(page, "29-home-alert-pill");
    await expectAxeClean(page, "home");

    // ── inbox: one simulated alert row
    await pill.click();
    await expect(page).toHaveURL(`${KEELSON}/alerts`);
    await expect(page.getByRole("heading", { level: 1, name: "Alerts" })).toBeVisible();
    const rows = page.getByRole("list", { name: "Alerts" }).locator("button[data-alert-id]");
    const row = rows.filter({ hasText: "KESTREL" });
    await expect(row).toHaveCount(1);
    await expect(row).toHaveAttribute("data-status", "new");
    await expect(row).toContainText("DEMO");
    await expect(row).toContainText(/Medium/i);
    await expect(row).toContainText(/Severity 6,055 bp meets rule/);
    await expect(row).toContainText(/2 ports|7 lanes|sites/);
    await keyScreenshot(page, "27-alerts-inbox");
    await expectAxeClean(page, "alerts-inbox");

    // ── drawer: why, exposure (map + list), sources with licence, updates
    await row.click();
    const drawer = page.getByRole("dialog", { name: /KESTREL/ });
    await expect(drawer).toBeVisible();
    const exposed = drawer.getByRole("list", { name: "Exposed sites and lanes" });
    for (const code of ["VNSGN", "F-VN-01", "SUP-VN-01", "SGN-HAM-SEA"]) await expect(exposed.getByText(code, { exact: true })).toBeVisible();
    const sources = drawer.getByRole("region", { name: "Sources" });
    await expect(sources).toContainText(/GDACS/);
    await expect(sources).toContainText(/CC BY/);
    await expect(drawer.getByRole("list", { name: "Updates" }).locator("li").first()).toBeVisible();
    await expect(drawer.getByText("DEMO").first()).toBeVisible();
    // the engine's map: footprint + exposed sites at the tenant's own positions
    await expect(drawer.getByText(/\d+ sites · \d+ at risk · 7 lanes/)).toBeVisible();
    await expect(drawer.locator("canvas").first()).toBeVisible({ timeout: 30_000 });
    await expect(drawer.getByText("Seen", { exact: true })).toBeVisible(); // opening marks it seen
    await page.waitForTimeout(3_000); // map tiles and the fit animation settle
    await keyScreenshot(page, "28-alert-drawer");
    await expectAxeClean(page, "alert-drawer");

    // ── Open case → the case page (its signal run is queued behind the held worker)
    await drawer.getByRole("button", { name: "Open case" }).click();
    await expect(page).toHaveURL(/\/cases\/[^/]+$/, { timeout: 30_000 });
    const caseId = caseIdFromUrl(page);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(/Tropical cyclone alert/);
    // the queued signal run shows as a waiting turn (announced by the case snapshot's pending_runs)
    await expect(page.locator("[data-run-id]").first()).toBeVisible();
    await expect(page.getByText("Getting started…")).toBeVisible();
    await page.waitForTimeout(800);
    await keyScreenshot(page, "30-case-from-alert");
    await expectAxeClean(page, "case-from-alert");

    // the alert now reads Opened and points at the case; opening again is idempotent (same case)
    const api = await keelsonApi("priya");
    try {
      const listed = await api.ctx.get("/api/alerts?limit=50");
      const alert = ((await listed.json()) as { items: { id: string; simulated: boolean; status: string; case_id: string | null }[] }).items.find(
        (a) => a.simulated,
      );
      expect(alert?.status).toBe("opened");
      expect(alert?.case_id).toBe(caseId);
      const again = await api.ctx.post(`/api/alerts/${alert!.id}/open-case`, {
        data: {},
        headers: { "X-CSRF-Token": api.csrf, "Idempotency-Key": crypto.randomUUID() },
      });
      expect(again.status()).toBe(200);
      const oc = (await again.json()) as { case_id: string; run_id: string; created: boolean };
      expect(oc.case_id).toBe(caseId);
      expect(oc.created).toBe(false);
      // never let the signal run reach a model: cancel it while the worker is held
      const cancel = await api.ctx.post(`/api/runs/${oc.run_id}/cancel`, { headers: { "X-CSRF-Token": api.csrf } });
      expect(cancel.status(), await cancel.text()).toBe(202);
    } finally {
      await api.ctx.dispose();
    }
    expect(errors, "page errors").toEqual([]);
  });
});
