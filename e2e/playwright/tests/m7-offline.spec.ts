import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { keelsonApi } from "./support/api";
import { KEELSON } from "./support/env";
import { expect, keyScreenshot, test } from "./support/fixtures";
import { signInAs } from "./support/keelson";
import { admin, raiseAutoCap, rejectPending, restoreSpec, setNoraRoles, setUpForeignErp, spec, tearDownForeignErp } from "./support/m7";

/**
 * M7 back-office in the browser without a model (docs/contracts/110 §1–§5, §7):
 * Pending changes (four-eyes: sam's guarded change waits, ava approves), Users, the custom
 * specialist builder (+ the playground's disabled state while the engine only replays recorded
 * answers) and the read-only workflow graph, the tier-A mapping editors + action dry-run on the
 * foreign ERP (Merxa /mcp-lite on northwind-test), Signals admin, and the Advisory badge of plan
 * blocks built on crawled data (block fixtures). Each page is axe-clean. Screenshots 31–36.
 *
 * State: the spec restores the demo-retail agent spec and the Signals watch regions it changed,
 * rejects any change request it leaves pending, gives nora back her planner-only roles and
 * disables the lite connector again.
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

function trackErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  return errors;
}


test.describe("M7 back-office (no model)", () => {
  test("Pending changes: sam's guarded change waits for a second admin; ava approves it", async ({ page }) => {
    test.setTimeout(3 * 60_000);
    const errors = trackErrors(page);
    const original = (await spec("sam")).version;
    try {
      const { lines } = await raiseAutoCap(3);
      await signInAs(page, "sam");
      await page.goto(`${KEELSON}/admin/changes`);
      await expect(page.getByRole("heading", { level: 1, name: "Pending changes" })).toBeVisible();
      const list = page.getByRole("list", { name: "Change requests" });
      const card = list.getByRole("region", { name: /Agent settings/ }).first();
      await expect(card).toBeVisible();
      await expect(card.getByText("Waiting for a second admin")).toBeVisible();
      await expect(card.getByRole("row", { name: new RegExp(`auto_max_lines Changed ${lines} ${lines + 3}`) })).toBeVisible();
      await expect(card.getByRole("button", { name: "Approve" }), "the requester never approves").toHaveCount(0);
      await expectAxeClean(page, "pending-changes-requester");

      await page.context().clearCookies();
      await signInAs(page, "ava");
      await page.goto(`${KEELSON}/admin/changes`);
      const nav = page.getByRole("navigation", { name: "Admin" });
      await expect(nav.getByRole("link", { name: /Pending changes.*1 waiting/ })).toBeVisible();
      const avaCard = page.getByRole("list", { name: "Change requests" }).getByRole("region", { name: /Agent settings/ }).first();
      await expect(avaCard.getByRole("button", { name: "Approve" })).toBeVisible();
      await expectAxeClean(page, "pending-changes-approver");
      await keyScreenshot(page, "31-pending-changes", { fullPage: true });

      await avaCard.getByRole("button", { name: "Approve" }).click();
      const dialog = page.getByRole("dialog", { name: "Approve this change?" });
      await expect(dialog).toBeVisible();
      await dialog.getByRole("textbox").fill("Reviewed in the e2e run.");
      await dialog.getByRole("button", { name: "Approve and apply" }).click();
      await expect(dialog).toBeHidden();
      await expect(page.getByText("Nothing waiting for approval")).toBeVisible();
      const after = await spec("sam");
      expect(after.version, "the approval applied a new spec version").not.toBe(original);
      expect(Number((after.spec["policy"] as Record<string, unknown>)["auto_max_lines"])).toBe(lines + 3);
      expect(errors, "no page errors").toEqual([]);
    } finally {
      await rejectPending("e2e clean-up");
      await restoreSpec(original);
    }
  });

  test("Users: the tenant's accounts and roles; invite and role dialogs", async ({ page }) => {
    const errors = trackErrors(page);
    await signInAs(page, "sam");
    await page.goto(`${KEELSON}/admin/users`);
    await expect(page.getByRole("heading", { level: 1, name: "Users" })).toBeVisible();
    const table = page.getByRole("table", { name: "Users" });
    for (const who of ["priya", "marco", "lena", "sam"]) await expect(table.getByRole("rowheader", { name: new RegExp(`\\b${who}$`) })).toBeVisible();
    // a tenant admin never changes a platform admin's account
    await expect(table.getByRole("button", { name: "Edit roles of ava" })).toBeDisabled();
    await expectAxeClean(page, "users");
    await keyScreenshot(page, "32-users", { fullPage: true });

    await page.getByRole("button", { name: "Invite user" }).click();
    const invite = page.getByRole("dialog", { name: "Invite a user" });
    await expect(invite).toBeVisible();
    await expectAxeClean(page, "users-invite");
    await invite.getByRole("button", { name: "Cancel" }).click();
    await table.getByRole("button", { name: "Edit roles of lena" }).click();
    const roles = page.getByRole("dialog", { name: /Roles of Lena/ });
    await expect(roles).toBeVisible();
    await roles.getByRole("button", { name: "Cancel" }).click();
    expect(errors, "no page errors").toEqual([]);
  });

  test("Agents: custom specialist builder (playground disabled on replay) and the workflow graph", async ({ page }) => {
    test.setTimeout(3 * 60_000);
    const errors = trackErrors(page);
    const original = (await spec("sam")).version;
    try {
      await signInAs(page, "sam");
      // The engine reports whether a playground turn can run; this run shows the state of an
      // installation that only replays recorded model answers (no model call is ever made here).
      const real = await page.request.get(`${KEELSON}/api/admin/agents/playground`);
      expect(real.status()).toBe(200);
      expect(Object.keys((await real.json()) as object)).toEqual(expect.arrayContaining(["available", "mode", "reason"]));
      await page.route("**/api/admin/agents/playground", async (route) => {
        if (route.request().method() !== "GET") return route.abort();
        await route.fulfill({ json: { available: false, mode: "replay", reason: "replay_only", message: "replay only" } });
      });

      await page.goto(`${KEELSON}/admin/agents/specialists`);
      await expect(page.getByRole("region", { name: "Custom specialists" })).toBeVisible();
      await page.getByRole("button", { name: "New specialist" }).click();
      const dialog = page.getByRole("dialog", { name: "New specialist" });
      await dialog.getByRole("textbox", { name: "Display name" }).fill("Supplier risk analyst");
      await dialog.getByRole("textbox", { name: "Id", exact: true }).fill("supplier_risk");
      await dialog.getByRole("textbox", { name: "What it checks" }).fill("Late or at-risk purchase orders and supplier messages for a style.");
      await dialog.getByRole("textbox", { name: "Role prompt" }).fill(
        "You check open purchase orders and supplier messages for the styles in the case. Report late POs with their codes.",
      );
      await dialog.getByRole("combobox", { name: "Model" }).selectOption({ label: "fast-default (chat)" });
      const tools = dialog.getByRole("group", { name: "Tools of Merxa Retail Suite" });
      await tools.getByRole("checkbox", { name: "list_purchase_orders", exact: true }).check();
      await tools.getByRole("checkbox", { name: /^list_supplier_messages/ }).check();
      await expect(dialog.getByRole("checkbox", { name: "create_change_set" }), "write tools are never offered").toBeDisabled();
      const tryIt = dialog.getByRole("region", { name: "Try it" });
      await tryIt.getByRole("textbox", { name: /^Ask / }).fill("Which trim POs for the Bangladesh styles are late?");
      await expect(tryIt.getByText(/only replays recorded model answers/)).toBeVisible();
      await expect(tryIt.getByRole("button", { name: "Run in playground" })).toBeDisabled();
      await expectAxeClean(page, "specialist-builder");
      await keyScreenshot(page, "33-agent-builder");
      await dialog.getByRole("button", { name: "Create specialist" }).click();
      await expect(dialog).toBeHidden();
      await expect(page.getByRole("region", { name: /Supplier risk analyst/ })).toBeVisible();
      const saved = await spec("sam");
      const custom = (saved.spec["custom_specialists"] as { slug: string; tools: { tool: string }[] }[]) ?? [];
      expect(custom.map((c) => c.slug)).toEqual(["supplier_risk"]);
      expect(custom[0]!.tools.map((t) => t.tool).sort()).toEqual(["list_purchase_orders", "list_supplier_messages"]);

      await page.goto(`${KEELSON}/admin/agents/workflow`);
      const graph = page.getByRole("group", { name: "Workflow graph (read-only)" });
      await expect(graph).toBeVisible();
      await expect(graph.getByRole("img", { name: "Edge from investigate to spec:supplier_risk" })).toBeAttached({ timeout: 30_000 });
      await expect(graph.getByRole("group", { name: /Supplier risk analyst/ })).toBeVisible();
      await expectAxeClean(page, "workflow-graph");
      await keyScreenshot(page, "34-workflow-graph", { fullPage: true });
      expect(errors, "no page errors").toEqual([]);
    } finally {
      await restoreSpec(original);
    }
  });

  test("Connectors: tier-A mapping editors and the action dry-run (foreign ERP, northwind-test)", async ({ page }) => {
    test.setTimeout(4 * 60_000);
    const errors = trackErrors(page);
    await setUpForeignErp();
    await setNoraRoles(["planner", "tenant-admin"]);
    try {
      await signInAs(page, "nora");
      await page.goto(`${KEELSON}/admin/connectors/merxa-lite/mappings`);
      await expect(page.getByRole("heading", { level: 1, name: /^Mappings · / })).toBeVisible();
      const caps = page.getByRole("region", { name: "Capabilities" });
      await expect(caps.getByText("One call per change, with a ledger and read-back")).toBeVisible();
      await expect(caps.getByRole("note")).toContainText("advisory");
      await expect(caps.getByRole("list", { name: "Mapped operations" }).getByRole("listitem")).toHaveCount(2);

      const read = page.getByRole("region", { name: "Read mapping" });
      await expect(read.getByText("Valid against the mapping contract.")).toBeVisible();
      await read.getByRole("button", { name: "Test with one page" }).click();
      await expect(read.getByText(/allocation/).first()).toBeVisible({ timeout: 60_000 });
      await expectAxeClean(page, "read-mapping");

      await page.getByRole("tab", { name: /^Action mapping/ }).click();
      const dry = page.getByRole("region", { name: "Dry run" });
      await expect(dry).toBeVisible();
      await dry.getByRole("combobox", { name: "Change" }).fill("transfer_order.create");
      await dry.getByRole("textbox", { name: "Sample payload (JSON)" }).fill(
        JSON.stringify({ from_dc: "DE1", to_dc: "NL1", ship_date: "2027-06-01", lines: [{ sku: "SKU-E2E-1", qty: 12 }] }, null, 2),
      );
      await dry.getByRole("button", { name: "Run dry run" }).click();
      const result = dry.locator("[data-dry-run-result]");
      await expect(result).toContainText("would call create_transfer_order", { timeout: 30_000 });
      await expect(result).toContainText("Not executed");
      await expect(result.getByLabel("Rendered arguments")).toContainText('"from_dc": "DE1"');
      await expect(result).toContainText("(query run)");
      await result.scrollIntoViewIfNeeded();
      await expectAxeClean(page, "action-dry-run");
      await keyScreenshot(page, "35-mapping-editor", { fullPage: true });

      await dry.getByRole("combobox", { name: "Change" }).fill("po_line.reschedule");
      await dry.getByRole("textbox", { name: "Sample payload (JSON)" }).fill('{ "eta_dc": "2027-06-01" }');
      await dry.getByRole("button", { name: "Run dry run" }).click();
      await expect(dry.locator("[data-dry-run-result]")).toContainText("is not mapped: it becomes a guided step");
      expect(errors, "no page errors").toEqual([]);
    } finally {
      await setNoraRoles(["planner"]);
      await tearDownForeignErp();
    }
  });

  test("Signals admin: sources with licences, a watch region round-trip, alert rules", async ({ page }) => {
    test.setTimeout(3 * 60_000);
    const errors = trackErrors(page);
    const sam = await keelsonApi("sam");
    const before = (await admin(sam, "GET", "/signals/watch-regions")) as { items: unknown[] };
    try {
      await signInAs(page, "sam");
      await page.goto(`${KEELSON}/admin/signals`);
      await expect(page.getByRole("heading", { level: 1, name: "Signals" })).toBeVisible();
      const sources = page.getByRole("table", { name: "Signal sources" });
      await expect(sources.getByRole("cell", { name: "CC BY 4.0" })).toBeVisible();
      await expect(sources.getByRole("switch").first()).toBeChecked();

      const regions = page.getByRole("region", { name: "Watch regions" });
      const form = regions.getByRole("form", { name: "Add a watch region" });
      await form.getByRole("textbox", { name: "Name" }).fill("E2E port watch");
      await form.getByRole("combobox", { name: "Site type" }).selectOption({ label: "Port" });
      const network = (await admin(sam, "GET", "/signals/network")) as { nodes: Record<string, string[]> };
      const port = (network.nodes["port"] ?? [])[0];
      expect(port, "a port in the network pushed to the hub").toBeTruthy();
      await form.getByRole("combobox", { name: "Site code" }).fill(port!);
      await form.getByRole("button", { name: "Add region" }).click();
      const row = regions.getByRole("row", { name: /E2E port watch/ });
      await expect(row).toBeVisible({ timeout: 20_000 });
      const stored = (await admin(sam, "GET", "/signals/watch-regions")) as { items: { name: string; kind: string; radius_km: number }[] };
      expect(stored.items.some((r) => r.name === "E2E port watch" && r.kind === "radius" && r.radius_km === 150)).toBe(true);

      await expect(page.getByRole("list", { name: "Alert rules in order" }).getByRole("textbox", { name: "Rule id" }).first()).toHaveValue(/.+/);
      await expectAxeClean(page, "signals-admin");
      await keyScreenshot(page, "36-signals-admin", { fullPage: true });
      expect(errors, "no page errors").toEqual([]);
    } finally {
      await admin(sam, "PUT", "/signals/watch-regions", { regions: before.items });
      await sam.ctx.dispose();
    }
  });

  test("Advisory badge on plan blocks built from crawled data (block fixtures)", async ({ page }) => {
    await signInAs(page, "priya");
    await page.goto(`${KEELSON}/dev/blocks`);
    for (const name of ["recommended_option.advisory", "scenario_compare.advisory", "narrative.advisory"]) {
      const section = page.locator("main section[aria-labelledby^='fx-']").filter({ has: page.getByRole("heading", { level: 2, name: new RegExp(name.replace(".", "\\.")) }) });
      await expect(section, name).toHaveCount(1);
      await expect(section.getByText(/Advisory/).first(), name).toBeVisible();
    }
    const option = page.locator("main section[aria-labelledby^='fx-']").filter({ has: page.getByRole("heading", { level: 2, name: /recommended_option\.advisory/ }) });
    await expect(option.getByText(/gathered by paging/i).first()).toBeVisible();
  });
});
