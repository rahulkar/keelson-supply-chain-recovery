import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { keelsonApi } from "./support/api";
import { KEELSON, MERXA } from "./support/env";
import { expect, keyScreenshot, test } from "./support/fixtures";
import { signInAs } from "./support/keelson";

/**
 * M2 back-office smoke (docs/contracts/70-backoffice-m2.md §6) as sam (tenant-admin, demo-retail):
 * Overview → Agents (edit the recommender's prompt addendum, save → new version) → Connectors
 * (Merxa with its capability tiers; the Add-connector wizard) → AI Gateway (models + usage), each
 * page axe-clean. Plus screenshots of the new Merxa pages (Sailings, API & MCP clients).
 * The spec is restored to the version it started from (via the API) so the demo stays unchanged.
 */

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

async function expectAxeClean(page: Page, where: string): Promise<void> {
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

type SpecEnvelope = { version: string };

async function currentSpecVersion(): Promise<string> {
  const api = await keelsonApi("sam");
  try {
    const r = await api.ctx.get("/api/admin/agents/spec");
    expect(r.status(), "GET /api/admin/agents/spec").toBe(200);
    return ((await r.json()) as SpecEnvelope).version;
  } finally {
    await api.ctx.dispose();
  }
}

/** Make `version` the active spec again (a no-op when nothing was saved). */
async function restoreSpec(version: string): Promise<void> {
  if ((await currentSpecVersion()) === version) return;
  const api = await keelsonApi("sam");
  try {
    const r = await api.ctx.post("/api/admin/agents/spec/restore", {
      data: { version },
      headers: { "X-CSRF-Token": api.csrf },
    });
    expect(r.status(), `restore ${version}: ${await r.text()}`).toBe(201);
  } finally {
    await api.ctx.dispose();
  }
}

test.describe("back-office (sam, tenant-admin)", () => {
  test("Overview, Agents, Connectors and AI Gateway work and are axe-clean", async ({ page }) => {
    test.setTimeout(4 * 60_000);
    const original = await currentSpecVersion();
    try {
      await signInAs(page, "sam");

      // Overview
      await page.goto(`${KEELSON}/admin`);
      await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
      const nav = page.getByRole("navigation", { name: "Admin" });
      for (const label of ["Overview", "AI Gateway", "Agents", "Connectors", "Audit"]) {
        await expect(nav.getByRole("link", { name: label, exact: true })).toBeVisible();
      }
      await expect(nav.getByRole("link", { name: "Tenants", exact: true })).toHaveCount(0); // platform-admin only
      await expectAxeClean(page, "admin-overview");

      // Agents: edit the recommender's prompt addendum and save a new version.
      await nav.getByRole("link", { name: "Agents", exact: true }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Agents" })).toBeVisible();
      // Role cards are titled with the display name (recommender = "Strategist", AGENT_DISPLAY_NAMES).
      const recommender = page.getByRole("region", { name: "Strategist", exact: true });
      await expect(recommender).toBeVisible({ timeout: 30_000 });
      const addendum = recommender.getByLabel("Prompt addendum");
      const text = `E2E ${Date.now()}: prefer options with fewer transfer lines when recovery is similar.`;
      await addendum.fill(text);
      await expect(page.getByRole("region", { name: "Save changes" })).toContainText("Unsaved changes");
      await expectAxeClean(page, "admin-agents-dirty");
      await page.getByRole("button", { name: "Review and save" }).click();
      const dialog = page.getByRole("dialog", { name: "Save a new version?" });
      await expect(dialog).toBeVisible();
      await expect(dialog).toContainText(text.slice(0, 30));
      await dialog.getByRole("button", { name: "Save version" }).click();
      await expect(dialog).toBeHidden();
      const saved = await currentSpecVersion();
      expect(saved, "a new spec version").not.toBe(original);
      await expect(page.getByText(`Saved as ${saved}`).first()).toBeVisible();
      await expect(page.getByRole("heading", { level: 1, name: "Agents" }).locator("..").locator("..")).toContainText(saved);
      await expect(page.getByRole("region", { name: "Save changes" })).toContainText("No unsaved changes");
      await expect(page.getByRole("region", { name: "Strategist", exact: true }).getByLabel("Prompt addendum")).toHaveValue(text);
      // The versions drawer lists the new version.
      await page.getByRole("button", { name: "Versions" }).click();
      const versions = page.getByRole("list", { name: "Saved versions" });
      await expect(versions).toContainText(saved);
      await keyScreenshot(page, "08-admin-agents");
      await page.keyboard.press("Escape");

      // Connectors: Merxa with its tiers; the Add-connector wizard.
      await nav.getByRole("link", { name: "Connectors", exact: true }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Connectors" })).toBeVisible();
      const row = page.getByRole("row").filter({ hasText: "Merxa Retail Suite" });
      await expect(row).toBeVisible({ timeout: 30_000 });
      const tiers = row.getByRole("list", { name: "Capability tiers" });
      for (const t of ["A", "B", "C", "W"]) await expect(tiers).toContainText(t);
      await expectAxeClean(page, "admin-connectors");
      await page.getByRole("link", { name: "Add connector" }).click();
      await expect(page.getByRole("heading", { level: 1, name: "Add connector" })).toBeVisible();
      const basics = page.getByRole("form", { name: "Basics" });
      await basics.getByLabel("Name").fill("Northwind ERP");
      await basics.getByLabel("Slug").fill("northwind-erp");
      await basics.getByLabel("MCP server URL").fill("https://erp.example.com/mcp");
      await expectAxeClean(page, "admin-connector-wizard");
      await keyScreenshot(page, "09-admin-connectors-wizard");

      // AI Gateway: models + usage.
      await nav.getByRole("link", { name: "AI Gateway", exact: true }).click();
      await expect(page.getByRole("heading", { level: 1, name: "AI Gateway" })).toBeVisible();
      const models = page.getByRole("table", { name: "Model aliases" });
      await expect(models).toBeVisible({ timeout: 30_000 });
      await expect(models).toContainText("fast-default");
      await expect(models).toContainText("reasoning-default");
      await expect(page.getByRole("heading", { name: "Usage, last 7 days" })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Budget and limits" })).toBeVisible();
      await expectAxeClean(page, "admin-gateway");
      // The admin pane scrolls inside the shell (fullPage would clip it): a tall viewport shows
      // models, budget and the usage charts in one picture.
      await page.setViewportSize({ width: 1280, height: 1500 });
      await page.getByRole("heading", { name: "Usage, last 7 days" }).scrollIntoViewIfNeeded();
      await keyScreenshot(page, "10-admin-gateway");
      await page.setViewportSize({ width: 1280, height: 800 });
    } finally {
      await restoreSpec(original);
    }
  });

  test("Knowledge: the seeded playbooks are indexed; upload, index and delete a document (axe-clean)", async ({ page }) => {
    // indexing the upload calls the embedding model (LiteLLM → Gemini): not part of a model-free run
    test.skip(!!process.env.E2E_NO_MODEL, "needs the live embedding model (E2E_NO_MODEL is set)");
    test.setTimeout(3 * 60_000);
    await signInAs(page, "sam");
    await page.goto(`${KEELSON}/admin/knowledge`);
    await expect(page.getByRole("heading", { level: 1, name: "Knowledge" })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Admin" }).getByRole("link", { name: "Knowledge", exact: true })).toBeVisible();
    // Bootstrap seeded the tenant's playbooks (contract 90 §7): five documents, all indexed.
    const kbs = page.getByRole("table", { name: "Knowledge bases" });
    await expect(kbs.getByRole("row").filter({ hasText: "Playbooks & policies" })).toBeVisible({ timeout: 30_000 });
    const docs = page.getByRole("table", { name: /^Documents in / });
    await expect(docs).toBeVisible({ timeout: 30_000 });
    for (const title of ["Peak trading continuity playbook", "Freight & expedite policy", "DC-to-DC transfer SOP"]) {
      await expect(docs.getByRole("row").filter({ hasText: title }).first()).toContainText("Ready");
    }
    await expect(page.getByRole("heading", { name: "Embedding profile" })).toBeVisible();
    await expect(page.locator("body")).toContainText("1,536");
    await expectAxeClean(page, "admin-knowledge");

    // Upload a small Markdown document through the file chooser → indexed (Ready) → delete it.
    const title = `E2E note ${Date.now()}`;
    await page.getByTestId("kb-file-input").setInputFiles({
      name: "e2e-note.md",
      mimeType: "text/markdown",
      buffer: Buffer.from(`# ${title}

Fictional test note: DC-to-DC transfers need a two-day notice.
`),
    });
    const row = docs.getByRole("row").filter({ hasText: /e2e-note|E2E note/ }).first();
    await expect(row).toContainText("Ready", { timeout: 60_000 });
    await keyScreenshot(page, "20-knowledge-admin");
    await row.getByRole("button", { name: /^Delete / }).click();
    const confirm = page.getByRole("alertdialog", { name: /^Delete / });
    await expect(confirm).toBeVisible();
    await confirm.getByRole("button", { name: "Delete document" }).click();
    await expect(docs.getByRole("row").filter({ hasText: /e2e-note|E2E note/ })).toHaveCount(0, { timeout: 30_000 });
  });

  test("planners see no back-office", async ({ page }) => {
    await signInAs(page, "priya");
    await page.goto(`${KEELSON}/admin`);
    await expect(page.getByRole("heading", { level: 1, name: "Admins only" })).toBeVisible();
    const r = await page.request.get(`${KEELSON}/api/admin/agents/spec`);
    expect(r.status()).toBe(403);
  });
});

test.describe("Merxa M2 pages", () => {
  test("Sailings & lanes and API & MCP clients render (merxa.admin)", async ({ page }) => {
    const r = await page.request.post(`${MERXA}/v1/auth/demo/login-as`, { data: { username: "merxa.admin" }, headers: { Origin: MERXA } });
    expect(r.status(), "Merxa login-as merxa.admin").toBe(200);

    await page.goto(`${MERXA}/sailings`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.locator("body")).toContainText(/Chittagong|BDCGP/);
    await expect(page.getByText(/couldn't|could not load|error/i)).toHaveCount(0);
    await keyScreenshot(page, "11-merxa-sailings");

    await page.goto(`${MERXA}/api-clients`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.locator("body")).toContainText("keelson-reader");
    await keyScreenshot(page, "12-merxa-api-clients");
  });
});
