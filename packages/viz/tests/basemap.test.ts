import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  BASEMAP_MODES,
  basemapAssetUrl,
  basemapBaseUrl,
  basemapChain,
  basemapManifestSchema,
  clearBasemapManifestCache,
  isBasemapMode,
  loadBasemapManifest,
  maxZoomFor,
  needsManifest,
  type BasemapManifest,
} from "../src/map/basemap";
import { configureNetworkMap, DEFAULT_NETWORK_MAP_CONFIG, networkMapConfig } from "../src/map/config";
import manifestFixture from "./fixtures/basemap-manifest.json";

const manifest = () => structuredClone(manifestFixture) as BasemapManifest;
const PAGE = "http://keelson.localhost/dev/blocks";

function respond(status: number, body: string | null, headers: Record<string, string> = {}) {
  return vi.fn<typeof fetch>(async () => new Response(body, { status, headers }));
}

beforeEach(() => clearBasemapManifestCache());
afterEach(() => {
  vi.useRealTimers();
  configureNetworkMap(DEFAULT_NETWORK_MAP_CONFIG);
});

describe("base map fallback chain", () => {
  it.each([
    ["auto", true, ["protomaps", "openfreemap", "offline", "plain"]],
    ["auto", false, ["openfreemap", "offline", "plain"]],
    ["protomaps", true, ["protomaps", "offline", "plain"]],
    ["protomaps", false, ["offline", "plain"]],
    ["openfreemap", true, ["openfreemap", "offline", "plain"]],
    ["openfreemap", false, ["openfreemap", "offline", "plain"]],
    ["offline", true, ["offline", "plain"]],
    ["offline", false, ["offline", "plain"]],
  ] as const)("%s with manifest=%s → %j", (mode, available, chain) => {
    expect(basemapChain(mode, available)).toEqual(chain);
  });

  it("protomaps mode never falls back to a third-party origin", () => {
    for (const available of [true, false]) expect(basemapChain("protomaps", available)).not.toContain("openfreemap");
  });

  it("knows its modes and which need the manifest", () => {
    expect(BASEMAP_MODES).toEqual(["auto", "protomaps", "openfreemap", "offline"]);
    expect(BASEMAP_MODES.filter(needsManifest)).toEqual(["auto", "protomaps"]);
    expect(isBasemapMode("offline")).toBe(true);
    expect(isBasemapMode("Offline")).toBe(false);
    expect(isBasemapMode("constructor")).toBe(false);
    expect(isBasemapMode(undefined)).toBe(false);
  });

  it("caps zoom per kind (over-zoom of the archive, the 1:110m outline)", () => {
    expect(maxZoomFor("protomaps", manifest())).toBe(11);
    expect(maxZoomFor("offline", null)).toBe(8);
    expect(maxZoomFor("openfreemap", null)).toBeUndefined();
    expect(maxZoomFor("plain", null)).toBeUndefined();
  });
});

describe("manifest schema (keelson.basemap/1)", () => {
  it("accepts the fixture the fetch tool writes (scripts/basemap buildManifest deep-equals it)", () => {
    expect(basemapManifestSchema.parse(manifestFixture)).toEqual(manifestFixture);
  });

  const invalid: [string, (m: Record<string, any>) => void][] = [
    ["an absolute archive URL", (m) => (m.archive.path = "https://evil.example/x.pmtiles")],
    ["a scheme-relative archive path", (m) => (m.archive.path = "//evil.example/x.pmtiles")],
    ["a leading slash", (m) => (m.archive.path = "/v4/x.pmtiles")],
    ["a dot-dot segment", (m) => (m.archive.path = "v4/../../etc/x.pmtiles")],
    ["a single-dot segment", (m) => (m.sprites.light = "./sprites/v4/light")],
    ["a backslash", (m) => (m.sprites.dark = "sprites\\v4\\dark")],
    ["percent-encoding", (m) => (m.sprites.dark = "sprites/%2e%2e/dark")],
    ["a non-pmtiles archive", (m) => (m.archive.path = "v4/x.json")],
    ["another schema", (m) => (m.schema = "keelson.basemap/2")],
    ["tiles v5", (m) => (m.source.basemap_version = "5.0.0")],
    ["tiles v3", (m) => (m.source.basemap_version = "3.4.1")],
    ["another source host", (m) => (m.source.url = "https://evil.example/20261009.pmtiles")],
    ["a glyphs URL", (m) => (m.glyphs = "https://fonts.example/{fontstack}/{range}.pbf")],
    ["an attribution field", (m) => (m.attribution = '<img src=x onerror="alert(1)">')],
    ["an extra archive key", (m) => (m.archive.url = "https://evil.example/x.pmtiles")],
    ["an unknown profile", (m) => (m.profile = "huge")],
    ["minzoom above maxzoom", (m) => (m.archive.minzoom = 7)],
    ["a bad sha256", (m) => (m.archive.sha256 = "abc")],
    ["a missing sprite theme", (m) => delete m.sprites.dark],
  ];
  it.each(invalid)("rejects %s", (_name, mutate) => {
    const m = manifest() as unknown as Record<string, any>;
    mutate(m);
    expect(basemapManifestSchema.safeParse(m).success).toBe(false);
  });
});

describe("loadBasemapManifest", () => {
  it("fetches same-origin, no redirects, no cache, and returns the manifest with its directory", async () => {
    const fetchImpl = respond(200, JSON.stringify(manifestFixture), { "Content-Type": "application/json" });
    const got = await loadBasemapManifest("/basemap/manifest.json", { fetchImpl, pageUrl: PAGE });
    expect(got).toEqual({ manifest: manifestFixture, baseUrl: "http://keelson.localhost/basemap/" });
    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe("http://keelson.localhost/basemap/manifest.json");
    expect(init).toMatchObject({ credentials: "same-origin", redirect: "error", cache: "no-cache" });
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it.each([
    ["404", respond(404, "not found")],
    ["204 (nothing installed)", respond(204, null)],
    ["an HTML page", respond(200, "<!doctype html><html></html>", { "Content-Type": "text/html" })],
    ["invalid JSON", respond(200, "{")],
    ["an invalid manifest", respond(200, JSON.stringify({ ...manifestFixture, schema: "other" }))],
    ["an oversized body", respond(200, " ".repeat(70_000))],
    ["a network error", vi.fn<typeof fetch>(async () => Promise.reject(new TypeError("Failed to fetch")))],
  ])("resolves to null for %s", async (_name, fetchImpl) => {
    await expect(loadBasemapManifest("/basemap/manifest.json", { fetchImpl, pageUrl: PAGE })).resolves.toBeNull();
  });

  it("gives up after the timeout", async () => {
    vi.useFakeTimers();
    const fetchImpl = vi.fn<typeof fetch>(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
        }),
    );
    const pending = loadBasemapManifest("/basemap/manifest.json", { fetchImpl, pageUrl: PAGE, timeoutMs: 3000 });
    await vi.advanceTimersByTimeAsync(3001);
    await expect(pending).resolves.toBeNull();
  });

  it("asks once per URL and page (cached, also when absent)", async () => {
    const fetchImpl = respond(404, null);
    await loadBasemapManifest("/basemap/manifest.json", { fetchImpl, pageUrl: PAGE });
    await loadBasemapManifest("/basemap/manifest.json", { fetchImpl, pageUrl: PAGE });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    clearBasemapManifestCache();
    await loadBasemapManifest("/basemap/manifest.json", { fetchImpl, pageUrl: PAGE });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("builds asset URLs under the manifest's directory, templates kept literal", () => {
    const base = basemapBaseUrl("/basemap/manifest.json", PAGE);
    expect(base).toBe("http://keelson.localhost/basemap/");
    expect(basemapAssetUrl(base, "fonts/{fontstack}/{range}.pbf")).toBe("http://keelson.localhost/basemap/fonts/{fontstack}/{range}.pbf");
    expect(basemapAssetUrl(base, manifest().archive.path)).toBe("http://keelson.localhost/basemap/v4/keelson-20261009-standard.pmtiles");
  });
});

describe("map settings", () => {
  it("default to the auto base map and the same-origin manifest; basemap merges like styleUrls", () => {
    expect(DEFAULT_NETWORK_MAP_CONFIG.basemap).toEqual({ mode: "auto", manifestUrl: "/basemap/manifest.json" });
    configureNetworkMap({ basemap: { mode: "offline" } });
    expect(networkMapConfig().basemap).toEqual({ mode: "offline", manifestUrl: "/basemap/manifest.json" });
    configureNetworkMap({ workerUrl: "/w.mjs" });
    expect(networkMapConfig().basemap.mode).toBe("offline");
    expect(networkMapConfig().styleUrls).toEqual(DEFAULT_NETWORK_MAP_CONFIG.styleUrls);
    configureNetworkMap(DEFAULT_NETWORK_MAP_CONFIG);
    expect(networkMapConfig()).toEqual(DEFAULT_NETWORK_MAP_CONFIG);
  });
});
