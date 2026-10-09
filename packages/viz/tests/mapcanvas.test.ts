import { afterEach, describe, expect, it, vi } from "vitest";
import type { BasemapManifest } from "../src/map/basemap";
import manifestFixture from "./fixtures/basemap-manifest.json";

const installed = { manifest: manifestFixture as BasemapManifest, baseUrl: "http://keelson.localhost/basemap/" };

afterEach(() => {
  vi.doUnmock("pmtiles");
  vi.resetModules();
});

describe("MapCanvas (module)", () => {
  it("falls back to a plain token-coloured background style when the base map can't load", async () => {
    const { fallbackStyle } = await import("../src/map/MapCanvas");
    const light = fallbackStyle("light");
    expect(light.version).toBe(8);
    expect(light.sources).toEqual({});
    expect(light.layers).toEqual([{ id: "background", type: "background", paint: { "background-color": "#f0f3f7" } }]);
    expect(fallbackStyle("dark").layers[0]).toMatchObject({ paint: { "background-color": expect.stringMatching(/^#[0-9a-f]{6}$/) } });
  });

  it("picks the style per base map kind", async () => {
    const { basemapStyle, fallbackStyle } = await import("../src/map/MapCanvas");
    const { offlineStyle } = await import("../src/map/offline");
    const { protomapsStyle } = await import("../src/map/protomaps");
    expect(basemapStyle("protomaps", "dark", installed)).toEqual(protomapsStyle("dark", installed));
    expect(basemapStyle("openfreemap", "light", null)).toBe("https://tiles.openfreemap.org/styles/positron");
    expect(basemapStyle("openfreemap", "dark", installed)).toBe("https://tiles.openfreemap.org/styles/dark");
    expect(basemapStyle("offline", "light", installed)).toEqual(offlineStyle("light"));
    expect(basemapStyle("plain", "light", installed)).toEqual(fallbackStyle("light"));
    // protomaps without an installed manifest never reaches the canvas; it would draw the outline
    expect(basemapStyle("protomaps", "light", null)).toEqual(offlineStyle("light"));
  });

  it("registers pmtiles:// once, without reading the archive's metadata", async () => {
    const ctor = vi.fn();
    vi.doMock("pmtiles", () => ({
      Protocol: class {
        tile = vi.fn();
        constructor(options: unknown) {
          ctor(options);
        }
      },
    }));
    const { ensurePmtilesProtocol } = await import("../src/map/pmtilesProtocol");
    const host = { addProtocol: vi.fn() };
    ensurePmtilesProtocol(host);
    ensurePmtilesProtocol(host);
    expect(ctor).toHaveBeenCalledTimes(1);
    expect(ctor).toHaveBeenCalledWith({ metadata: false });
    expect(host.addProtocol).toHaveBeenCalledTimes(1);
    expect(host.addProtocol).toHaveBeenCalledWith("pmtiles", expect.any(Function));
  });
});
