import { describe, expect, it } from "vitest";
import nextConfig, { MAP_TILE_ORIGINS } from "../next.config";

async function csp(): Promise<string> {
  const rules = await nextConfig.headers!();
  const header = rules[0]!.headers.find((h) => h.key === "Content-Security-Policy");
  return header!.value;
}

describe("Content-Security-Policy (M3 network map)", () => {
  it("allows the base-map tile origin for fetches and images, nothing else off-origin", async () => {
    const policy = await csp();
    expect(MAP_TILE_ORIGINS).toEqual(["https://tiles.openfreemap.org"]);
    expect(policy).toMatch(/connect-src 'self' https:\/\/tiles\.openfreemap\.org(;| ws:)/);
    expect(policy).toContain("img-src 'self' data: blob: https://tiles.openfreemap.org");
    expect(policy).toContain("script-src 'self' 'unsafe-inline'");
  });

  it("MapLibre's worker is same-origin only (no blob: workers)", async () => {
    const policy = await csp();
    expect(policy).toContain("worker-src 'self'");
    expect(policy).not.toMatch(/worker-src[^;]*blob:/);
  });
});
