import { afterEach, describe, expect, it, vi } from "vitest";

/** next.config.ts evaluated with `KEELSON_CSP_MAP_ORIGINS` as at build time (undefined = unset). */
async function load(origins?: string) {
  vi.resetModules();
  vi.stubEnv("KEELSON_CSP_MAP_ORIGINS", origins ?? "");
  return import("../next.config");
}

async function csp(origins?: string): Promise<string> {
  const { default: nextConfig } = await load(origins);
  const rules = await nextConfig.headers!();
  const header = rules[0]!.headers.find((h) => h.key === "Content-Security-Policy");
  return header!.value;
}

const directive = (policy: string, name: string) => policy.split("; ").find((d) => d.startsWith(`${name} `));

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("Content-Security-Policy (M3 network map, M8 base map knob)", () => {
  it("by default allows the OpenFreeMap origin for fetches and images, nothing else off-origin", async () => {
    const policy = await csp();
    const { MAP_TILE_ORIGINS } = await load();
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

  it("KEELSON_CSP_MAP_ORIGINS=none builds a strict policy: the self-hosted base map is same-origin", async () => {
    const policy = await csp("none");
    expect(directive(policy, "img-src")).toBe("img-src 'self' data: blob:");
    // tests run with NODE_ENV=test: `next dev`'s HMR websocket is allowed outside production
    expect(directive(policy, "connect-src")).toBe("connect-src 'self' ws: wss:");
    expect(policy).not.toContain("openfreemap");
    expect(directive(policy, "worker-src")).toBe("worker-src 'self'");
  });

  it("takes several https origins", async () => {
    const policy = await csp(" https://tiles.example.com  https://maps.example.org:8443 ");
    expect(directive(policy, "img-src")).toBe("img-src 'self' data: blob: https://tiles.example.com https://maps.example.org:8443");
    expect(directive(policy, "connect-src")).toMatch(/^connect-src 'self' https:\/\/tiles\.example\.com https:\/\/maps\.example\.org:8443( ws: wss:)?$/);
  });

  it.each([
    "http://tiles.example.com",
    "https://tiles.example.com/styles",
    "https://*.example.com",
    "tiles.example.com",
    "https://tiles.example.com;script-src *",
    "'unsafe-inline'",
    "https://user@tiles.example.com",
  ])("refuses %j (the build fails)", async (bad) => {
    await expect(load(bad)).rejects.toThrow(/KEELSON_CSP_MAP_ORIGINS/);
  });
});
