import { describe, expect, it } from "vitest";

describe("MapCanvas (module)", () => {
  it("falls back to a plain token-coloured background style when the base map can't load", async () => {
    const { fallbackStyle } = await import("../src/map/MapCanvas");
    const light = fallbackStyle("light");
    expect(light.version).toBe(8);
    expect(light.sources).toEqual({});
    expect(light.layers).toEqual([{ id: "background", type: "background", paint: { "background-color": "#f0f3f7" } }]);
    expect(fallbackStyle("dark").layers[0]).toMatchObject({ paint: { "background-color": expect.stringMatching(/^#[0-9a-f]{6}$/) } });
  });
});
