import { hex, hexColor, type ColorRole, type ThemeName } from "@keelson/tokens";
import { wcagContrast } from "culori";
import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import type { BasemapManifest, InstalledBasemap } from "../src/map/basemap";
import { NATURAL_EARTH_ATTRIBUTION, offlineStyle } from "../src/map/offline";
import { basemapPalette, basemapRoles } from "../src/map/palette";
import { keelsonFlavor, PROTOMAPS_ATTRIBUTION, protomapsStyle } from "../src/map/protomaps";
import { NODE_STATUSES, statusMeta } from "../src/lib/status";
import manifestFixture from "./fixtures/basemap-manifest.json";

const THEMES: ThemeName[] = ["light", "dark"];
const BASE = "http://keelson.localhost/basemap/";
const installed: InstalledBasemap = { manifest: manifestFixture as BasemapManifest, baseUrl: BASE };

/** Canonical JSON (sorted keys) → sha256: the style is pure, so the hash pins it per theme. */
function canonical(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") {
    return `{${Object.keys(v)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical((v as Record<string, unknown>)[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(v);
}
const sha256 = (s: string) => createHash("sha256").update(s).digest("hex");

/** Every string value anywhere in a JSON value. */
function strings(v: unknown, out: string[] = []): string[] {
  if (typeof v === "string") out.push(v);
  else if (Array.isArray(v)) v.forEach((x) => strings(x, out));
  else if (v && typeof v === "object") Object.values(v).forEach((x) => strings(x, out));
  return out;
}

const OPERATORS = new Set(["case", "==", "!=", "get", "has", "step", "zoom", "match", "coalesce", "all", "any", "!", "interpolate", "format", "to-string"]);
/** Font names in a `text-font` value: plain font stacks and `["literal", [...]]` inside expressions. */
function fontsOf(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  if (v[0] === "literal") return Array.isArray(v[1]) ? (v[1] as string[]) : [];
  if (typeof v[0] === "string" && OPERATORS.has(v[0])) return v.slice(1).flatMap(fontsOf);
  return v.every((x) => typeof x === "string") ? (v as string[]) : v.flatMap(fontsOf);
}
/** `text-font` values anywhere in a layer (layout and `format` sections inside `text-field`). */
function textFonts(v: unknown, out: string[] = []): string[] {
  if (Array.isArray(v)) v.forEach((x) => textFonts(x, out));
  else if (v && typeof v === "object") {
    for (const [k, x] of Object.entries(v)) {
      if (k === "text-font") out.push(...fontsOf(x));
      else textFonts(x, out);
    }
  }
  return out;
}

const contrast = (a: string, b: string) => wcagContrast(a, b);

describe("Keelson Protomaps style", () => {
  it.each(THEMES)("is deterministic (%s)", (theme) => {
    const a = canonical(protomapsStyle(theme, installed));
    expect(canonical(protomapsStyle(theme, structuredClone(installed)))).toBe(a);
    // Pinned: a @protomaps/basemaps bump (Renovate) changes this on purpose — review the diff, then update.
    expect(sha256(a)).toBe(STYLE_SHA256[theme]);
  });

  it.each(THEMES)("loads everything from the manifest's own directory (%s)", (theme) => {
    const style = protomapsStyle(theme, installed);
    expect(style.glyphs).toBe(`${BASE}fonts/{fontstack}/{range}.pbf`);
    expect(style.sprite).toBe(`${BASE}sprites/v4/${theme}`);
    expect(style.sources).toEqual({
      protomaps: { type: "vector", url: `pmtiles://${BASE}v4/keelson-20261009-standard.pmtiles`, attribution: PROTOMAPS_ATTRIBUTION },
    });
    const urls = strings(style).filter((s) => /^[a-z][a-z0-9+.-]*:/i.test(s) && s.includes("/"));
    expect(urls.length).toBeGreaterThanOrEqual(3);
    for (const u of urls) expect(u.startsWith(BASE) || u.startsWith(`pmtiles://${BASE}`), u).toBe(true);
  });

  it.each(THEMES)("uses only installed fonts, has unique layer ids and no POI / house-number layers (%s)", (theme) => {
    const style = protomapsStyle(theme, installed);
    const ids = style.layers.map((l) => l.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids.filter((id) => id.startsWith("pois") || id.startsWith("address"))).toEqual([]);
    expect(ids).toEqual(expect.arrayContaining(["background", "earth", "water", "boundaries_country", "places_country", "places_locality"]));
    const fonts = new Set(style.layers.flatMap((l) => textFonts(l)));
    expect(fonts.size).toBeGreaterThan(0);
    for (const f of fonts) expect(manifestFixture.fonts, f).toContain(f);
  });

  it.each(THEMES)("attribution is the constant, never data (%s)", (theme) => {
    const style = protomapsStyle(theme, installed);
    const attributions = strings(style).filter((s) => s.includes("<a "));
    expect(attributions).toEqual([PROTOMAPS_ATTRIBUTION]);
    expect(PROTOMAPS_ATTRIBUTION).toContain('href="https://www.openstreetmap.org/copyright"');
  });

  it.each(THEMES)("every flavour colour is a Keelson token (%s)", (theme) => {
    const tokenHex = new Set<string>(Object.values(hex[theme]));
    const flavor = keelsonFlavor(theme) as unknown as Record<string, unknown>;
    expect(flavor.pois).toBeUndefined();
    expect(flavor.landcover).toBeUndefined();
    for (const [field, value] of Object.entries(flavor)) {
      if (typeof value !== "string") continue;
      expect(tokenHex.has(value), `${field} = ${value}`).toBe(true);
    }
  });
});

describe("base map contrast (WCAG, tokens)", () => {
  it.each(THEMES)("labels ≥ 4.5:1 on the colour they sit on (%s)", (theme) => {
    const f = keelsonFlavor(theme);
    for (const label of [f.country_label, f.city_label, f.state_label, f.subplace_label, f.roads_label_major, f.roads_label_minor]) {
      expect(contrast(label, f.earth)).toBeGreaterThanOrEqual(4.5);
    }
    expect(contrast(f.ocean_label, f.water)).toBeGreaterThanOrEqual(4.5);
    // halos are the land colour, so labels keep their contrast over roads
    expect(new Set([f.city_label_halo, f.state_label_halo, f.subplace_label_halo, f.roads_label_major_halo])).toEqual(new Set([f.earth]));
  });

  it.each(THEMES)("land and water are distinguishable but calm (%s)", (theme) => {
    const p = basemapPalette(theme);
    const ratio = contrast(p.earth, p.water);
    expect(ratio).toBeGreaterThanOrEqual(1.08);
    expect(ratio).toBeLessThanOrEqual(1.6);
  });

  it.each(THEMES)("never makes the overlay harder to see than the M3 plain background (%s)", (theme) => {
    const p = basemapPalette(theme);
    const plain = hexColor("surface-2", theme);
    const overlay: ColorRole[] = [...NODE_STATUSES.map((s) => statusMeta[s].color), "chart-1", "chart-2", "chart-3", "chart-4", "chart-5", "chart-6"];
    for (const role of overlay) {
      const c = hexColor(role, theme);
      const floor = contrast(c, plain) - 0.01;
      expect(contrast(c, p.earth), `${role} on land`).toBeGreaterThanOrEqual(floor);
      expect(contrast(c, p.water), `${role} on water`).toBeGreaterThanOrEqual(floor);
    }
  });

  it("palette roles are tokens of each theme", () => {
    for (const theme of THEMES) for (const role of Object.values(basemapRoles(theme))) expect(hex[theme]).toHaveProperty(role);
  });
});

describe("offline outline (Natural Earth 1:110m)", () => {
  // vitest runs in the package directory (pnpm --filter @keelson/viz test)
  const ASSET = path.join(process.cwd(), "src", "map", "assets", "ne-110m-land.json");

  it("is small, detailed enough and rounded to 2 decimals", () => {
    expect(statSync(ASSET).size).toBeLessThanOrEqual(80 * 1024);
    const land = JSON.parse(readFileSync(ASSET, "utf8")) as GeoJSON.FeatureCollection<GeoJSON.MultiPolygon>;
    expect(land.features).toHaveLength(1);
    const polygons = land.features[0]!.geometry.coordinates;
    expect(polygons.length).toBeGreaterThan(100);
    for (const ring of polygons.flat()) {
      expect(ring.length).toBeGreaterThanOrEqual(4);
      expect(ring[0]).toEqual(ring.at(-1));
      for (const [x, y] of ring) {
        expect(Math.round(x! * 100) / 100).toBe(x);
        expect(Math.round(y! * 100) / 100).toBe(y);
        expect(Math.abs(x!)).toBeLessThanOrEqual(180);
        expect(Math.abs(y!)).toBeLessThanOrEqual(90);
      }
    }
  });

  it.each(THEMES)("draws inline data in token colours with the Natural Earth credit (%s)", (theme) => {
    const style = offlineStyle(theme);
    const p = basemapPalette(theme);
    expect(style.glyphs).toBeUndefined();
    expect(style.sprite).toBeUndefined();
    const land = style.sources.land as { type: string; data: unknown; attribution: string };
    expect(land.type).toBe("geojson");
    expect(typeof land.data).toBe("object"); // inline: no URL, nothing to fetch
    expect(land.attribution).toBe(NATURAL_EARTH_ATTRIBUTION);
    expect(style.layers.map((l) => l.id)).toEqual(["background", "land", "coastline"]);
    expect(style.layers[0]).toMatchObject({ paint: { "background-color": p.water } });
    expect(style.layers[1]).toMatchObject({ paint: { "fill-color": p.earth } });
    expect(strings(style).filter((s) => /^https?:/.test(s))).toEqual([]);
  });
});

const STYLE_SHA256: Record<ThemeName, string> = {
  light: "e36c6a159f0acabbec9ea047a2005de6713f520a4e5579644b2a8670fe8c0b8f",
  dark: "41e11f041cd2801ed31ecd209e5a8ed9fc04094b9eecf2ab2fca6cc611804c50",
};
