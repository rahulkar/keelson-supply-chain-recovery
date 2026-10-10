import { describe, expect, it, vi } from "vitest";
import { basemapModeFromEnv } from "./basemap";

describe("KEELSON_BASEMAP (runtime base map mode)", () => {
  it.each([
    ["auto", "auto"],
    ["protomaps", "protomaps"],
    ["openfreemap", "openfreemap"],
    ["offline", "offline"],
    [" Offline ", "offline"],
    ["PROTOMAPS", "protomaps"],
  ] as const)("%j → %s", (raw, mode) => {
    expect(basemapModeFromEnv(raw)).toBe(mode);
  });

  it.each([undefined, "", "none", "google", "constructor", "__proto__", "auto,offline"])("%j falls back to auto", (raw) => {
    expect(basemapModeFromEnv(raw)).toBe("auto");
  });

  it("reads process.env by default", () => {
    vi.stubEnv("KEELSON_BASEMAP", "offline");
    try {
      expect(basemapModeFromEnv()).toBe("offline");
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
