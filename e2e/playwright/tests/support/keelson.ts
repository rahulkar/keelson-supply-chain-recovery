import { expect, type Locator, type Page } from "@playwright/test";
import { AUTH, KEELSON, RUN_TIMEOUT_MS, demoPassword } from "./env";
import type { Tracer } from "./fixtures";

/** Keycloak (keelson or merxa realm) login form: username, then the password with tracing paused. */
export async function keycloakLogin(page: Page, tracer: Tracer, realm: "keelson" | "merxa", username: string): Promise<void> {
  await expect(page).toHaveURL(new RegExp(`^${AUTH.replace(/\./g, "\\.")}/realms/${realm}/`), { timeout: 30_000 });
  const user = page.locator("#username");
  await expect(user).toBeVisible();
  // The Connect flow sends login_hint (prefilled and sometimes read-only); only type when editable.
  if ((await user.inputValue()) !== username && (await user.isEditable())) await user.fill(username);
  await tracer.secret(async () => {
    await page.locator("#password").fill(demoPassword());
    await page.locator("#kc-login").click();
    // Leave the Keycloak page entirely before tracing resumes (its DOM still holds the field).
    await page.waitForURL((url) => !url.href.startsWith(AUTH), { timeout: 30_000, waitUntil: "commit" });
  });
}

/** Header persona switcher (DEMO): open, pick `name`, wait for the full reload. */
export async function switchPersona(page: Page, name: string): Promise<void> {
  const trigger = page.getByRole("button", { name: /^Demo persona: .*Switch persona$/ });
  await trigger.click();
  const item = page.getByRole("menuitem", { name: new RegExp(name) });
  await expect(item).toBeVisible();
  await Promise.all([page.waitForEvent("load"), item.click()]);
  await expect(page.getByRole("button", { name: new RegExp(`^Demo persona: ${name}\\. Switch persona$`) })).toBeVisible();
}

export function caseIdFromUrl(page: Page): string {
  const m = new URL(page.url()).pathname.match(/^\/cases\/([^/]+)$/);
  if (!m?.[1]) throw new Error(`not on a case page: ${page.url()}`);
  return decodeURIComponent(m[1]);
}

/** All turns of the thread (one `<section>` per run, labelled "Turn N: …"). */
export function turns(page: Page): Locator {
  return page.locator("main section[aria-labelledby^='turn-']");
}

export function timeline(page: Page, turn: number): Locator {
  return page.getByRole("region", { name: `Thinking, turn ${turn}`, exact: true });
}

export function block(scope: Page | Locator, kind: string): Locator {
  return scope.locator(`[data-block-kind="${kind}"]`);
}

/** Wait for a block kind anywhere in the thread (not the canvas) — long timeout for LLM runs. */
export async function waitForBlock(page: Page, kind: string, timeout = RUN_TIMEOUT_MS): Promise<Locator> {
  const loc = block(turns(page), kind).last();
  await expect(loc).toBeVisible({ timeout });
  return loc;
}

export function canvas(page: Page): Locator {
  return page.getByRole("complementary", { name: "Canvas" }).or(page.getByRole("dialog", { name: "Canvas" }));
}

export type CanvasVisualKind = "scenario_compare" | "network_map" | "impact_graph" | "stockout_timeline";

/**
 * The M3 canvas visuals of the newest result, each waited for until it has actually drawn (they
 * are lazy-loaded client components and fetch their `data_ref` payload first): the option
 * comparison and the stock-out timeline render their ECharts SVG, the impact graph lays out its
 * React Flow nodes, the network map has its MapLibre canvas (it needs WebGL; without it the block
 * falls back to tables, which fails here). Opens the canvas (Result tab) when it is closed.
 */
export async function canvasVisuals(page: Page, panel: Locator, timeout = 60_000): Promise<Record<CanvasVisualKind, Locator>> {
  if (!(await panel.isVisible())) await page.getByRole("button", { name: "Open canvas" }).click();
  await expect(panel).toBeVisible();
  const result = panel.getByRole("tab", { name: /^Result/ });
  if ((await result.count()) > 0 && (await result.getAttribute("aria-selected")) !== "true") await result.click();
  const visuals = {
    scenario_compare: block(panel, "scenario_compare").last(),
    network_map: block(panel, "network_map").last(),
    impact_graph: block(panel, "impact_graph").last(),
    stockout_timeline: block(panel, "stockout_timeline").last(),
  };
  for (const loc of Object.values(visuals)) await expect(loc).toBeVisible({ timeout });
  await expect(visuals.scenario_compare.locator('[data-chart] svg').first()).toBeVisible({ timeout });
  await expect(visuals.stockout_timeline.locator('[data-chart] svg').first()).toBeVisible({ timeout });
  await visuals.impact_graph.scrollIntoViewIfNeeded();
  await expect(visuals.impact_graph.locator(".react-flow__node").first()).toBeVisible({ timeout });
  await visuals.network_map.scrollIntoViewIfNeeded();
  await expect(visuals.network_map.getByText(/can.t draw the map/), "WebGL map (not the table fallback)").toHaveCount(0);
  await expect(visuals.network_map.locator("[data-map-canvas] canvas").first()).toBeVisible({ timeout });
  // Base-map tiles and the deck.gl overlay settle after the first frame.
  await page.waitForTimeout(3_000);
  return visuals;
}

/**
 * Tool names an LLM specialist calls (Merxa MCP), raw or merely de-underscored ("Checking get
 * inventory in Merxa"); users must only ever see sentences ("Read 8 inventory positions …").
 */
export const RAW_TOOL_NAME = new RegExp(
  [
    String.raw`\b[a-z]+_[a-z_]+\b`,
    String.raw`\b(?:get|list|search|create|add|validate|apply|rollback)[ _](?:[a-z]+[ _]){0,3}?(?:inventory|forecast|orders?|allocations|locations|stores|entity|entities|bom|messages|set|trail|guide)\b`,
    String.raw`\bmcp\b`,
    "structuredContent",
  ].join("|"),
  "i",
);

/** Expand every thinking timeline and every agent row (one at a time: labels change on click). */
export async function expandAllThinking(page: Page): Promise<void> {
  const collapsed = page.getByRole("button", { name: /Show thinking/ });
  for (let i = 0; i < 20 && (await collapsed.count()) > 0; i++) await collapsed.first().click();
  const agents = page.locator("li[data-agent] button[aria-expanded='false']");
  for (let i = 0; i < 40 && (await agents.count()) > 0; i++) await agents.first().click();
}

/** No inline turn shows more than two cards (calm thread, `30-engine-api-events.md`). */
export async function expectCalmTurns(page: Page): Promise<void> {
  const count = await turns(page).count();
  for (let i = 0; i < count; i++) {
    const inline = await turns(page).nth(i).locator("[data-block-kind]").count();
    expect(inline, `turn ${i + 1} shows ${inline} inline cards`).toBeLessThanOrEqual(2);
  }
}

/**
 * Fast sign-in for tests that are not about signing in: demo login-as through the page's own
 * request context (shares the browser context's cookie jar).
 */
export async function signInAs(page: Page, username: string): Promise<void> {
  const r = await page.request.post(`${KEELSON}/api/demo/login-as`, { data: { username }, headers: { Origin: KEELSON } });
  expect(r.status(), `login-as ${username}`).toBe(200);
}

/** No horizontal page scroll, and nothing visible sticks out of the viewport. */
export async function expectNoHorizontalOverflow(page: Page, where: string): Promise<void> {
  const result = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const offenders: string[] = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const s = getComputedStyle(el);
      if (s.visibility === "hidden" || s.position === "fixed") continue;
      // Content inside a horizontal scroller (tables) is fine; the scroller itself must fit.
      let p = el.parentElement;
      let clipped = false;
      while (p && p !== document.body) {
        const ps = getComputedStyle(p);
        if (ps.overflowX !== "visible") {
          clipped = true;
          break;
        }
        p = p.parentElement;
      }
      if (clipped) continue;
      if (r.right > vw + 1 || r.left < -1) {
        const label = `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}.${(el.className && typeof el.className === "string" ? el.className : "").split(" ").slice(0, 3).join(".")}`;
        offenders.push(`${label} [${Math.round(r.left)}..${Math.round(r.right)}]`);
      }
    }
    return { scroll: document.documentElement.scrollWidth, vw, offenders: offenders.slice(0, 8) };
  });
  expect(result.scroll, `${where}: page scrolls horizontally`).toBeLessThanOrEqual(result.vw);
  expect(result.offenders, `${where}: elements outside the viewport`).toEqual([]);
}
