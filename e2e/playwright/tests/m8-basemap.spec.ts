import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { KEELSON } from "./support/env";
import { elementScreenshot, expect, test } from "./support/fixtures";
import { signInAs } from "./support/keelson";

/**
 * M8 self-hosted base map (docs/contracts/127-m8-basemap.md), no model calls: the network map
 * fixture on /dev/blocks with every request that would leave *.localhost blocked.
 *
 * - Base map installed (`docker compose --profile basemap run --rm basemap-fetch --profile lite`):
 *   GET /basemap/manifest.json is 200 → the map draws the self-hosted Protomaps tiles
 *   (`data-map-basemap="protomaps"`) read with HTTP Range (206 from /basemap/v4/) and attempts no
 *   third-party request at all.
 * - Nothing installed (manifest 204): OpenFreeMap is blocked here, so the chain ends at the built-in
 *   Natural Earth outline (`data-map-basemap="offline"`) with the "simplified background map" notice.
 *
 * Both: a drawn canvas, never the "couldn't be loaded" notice, attribution visible with its credit
 * link, axe-clean, no page errors — in the light and the dark theme. Screenshots 37-basemap(-dark).
 * Expects the default runtime mode (KEELSON_BASEMAP=auto).
 */

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];
const SECTION = "main section[aria-labelledby='fx-network_map']";

async function basemapInstalled(page: Page): Promise<boolean> {
  const r = await page.request.get(`${KEELSON}/basemap/manifest.json`);
  if (r.status() !== 200) return false;
  const body = (await r.json()) as { schema?: string };
  return body.schema === "keelson.basemap/1";
}

function isLocal(url: URL): boolean {
  return url.hostname === "localhost" || url.hostname.endsWith(".localhost");
}

test.describe("M8 self-hosted base map (offline, no model)", () => {
  for (const scheme of ["light", "dark"] as const) {
    test(`the network map draws a base map with nothing from third parties (${scheme})`, async ({ page, context }) => {
      test.setTimeout(3 * 60_000);
      const offOrigin: string[] = [];
      await context.route(
        (url) => !isLocal(url),
        (route) => {
          offOrigin.push(new URL(route.request().url()).host);
          return route.abort("blockedbyclient");
        },
      );
      const archive: number[] = [];
      page.on("response", (r) => {
        if (new URL(r.url()).pathname.startsWith("/basemap/v4/")) archive.push(r.status());
      });
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));

      await page.emulateMedia({ colorScheme: scheme });
      await signInAs(page, "priya");
      const installed = await basemapInstalled(page);
      test.info().annotations.push({ type: "note", description: installed ? "self-hosted base map installed" : "no base map installed" });

      await page.goto(`${KEELSON}/dev/blocks`);
      await expect(page.getByRole("heading", { level: 1, name: "Render blocks" })).toBeVisible();
      await expect(page.locator("html")).toHaveAttribute("data-theme", scheme);
      const section = page.locator(SECTION);
      await section.scrollIntoViewIfNeeded();
      const map = section.locator("[data-map-canvas]");
      await expect(map.locator("canvas").first()).toBeVisible({ timeout: 60_000 });

      if (installed) {
        await expect(map).toHaveAttribute("data-map-basemap", "protomaps", { timeout: 30_000 });
        await expect.poll(() => archive.filter((s) => s === 206).length, { timeout: 30_000 }).toBeGreaterThan(0);
        await expect(section.getByText(/Showing a simplified background map/)).toHaveCount(0);
      } else {
        await expect(map).toHaveAttribute("data-map-basemap", "offline", { timeout: 30_000 });
        await expect(section.getByText(/Showing a simplified background map/)).toBeVisible();
      }
      await expect(section.getByText(/background map couldn.t be loaded/)).toHaveCount(0);
      await expect(map.locator("[data-map-style]")).toHaveAttribute("data-map-style", scheme);

      const attribution = map.locator(".maplibregl-ctrl-attrib");
      await expect(attribution).toBeVisible();
      await expect(attribution.getByRole("link", { name: installed ? "OpenStreetMap" : "Natural Earth" })).toBeVisible();

      const results = await new AxeBuilder({ page }).include(SECTION).withTags(TAGS).analyze();
      const serious = results.violations
        .filter((v) => v.impact === "serious" || v.impact === "critical")
        .map((v) => ({ id: v.id, impact: v.impact, targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")) }));
      expect(serious, "serious/critical axe violations on the map").toEqual([]);

      await elementScreenshot(section, scheme === "dark" ? "37-basemap-dark" : "37-basemap");
      if (installed) expect(offOrigin, "requests that tried to leave the origin").toEqual([]);
      expect(errors, "page errors").toEqual([]);
    });
  }
});
