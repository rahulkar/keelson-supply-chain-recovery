/**
 * The offline base map: Natural Earth 1:110m land (public domain) committed as a ~76 KB GeoJSON
 * (`assets/ne-110m-land.json`, built by `scripts/basemap/build-land-fallback.mjs`) and drawn in
 * token colours. Inline data, no glyphs, no sprite: it needs no server at all. Imported only by the
 * lazy MapCanvas chunk.
 */
import { hexColor, type ThemeName } from "@keelson/tokens";
import type { StyleSpecification } from "maplibre-gl";
import land from "./assets/ne-110m-land.json";
import { basemapPalette } from "./palette";

/** Courtesy credit (Natural Earth is public domain). A constant: MapLibre renders attribution as HTML. */
export const NATURAL_EARTH_ATTRIBUTION = 'Made with <a href="https://www.naturalearthdata.com/">Natural Earth</a>';

export function offlineStyle(theme: ThemeName): StyleSpecification {
  const p = basemapPalette(theme);
  return {
    version: 8,
    sources: {
      land: { type: "geojson", data: land as GeoJSON.FeatureCollection, attribution: NATURAL_EARTH_ATTRIBUTION },
    },
    layers: [
      { id: "background", type: "background", paint: { "background-color": p.water } },
      { id: "land", type: "fill", source: "land", paint: { "fill-color": p.earth, "fill-antialias": true } },
      { id: "coastline", type: "line", source: "land", paint: { "line-color": hexColor("border-strong", theme), "line-width": 0.5 } },
    ],
  };
}
