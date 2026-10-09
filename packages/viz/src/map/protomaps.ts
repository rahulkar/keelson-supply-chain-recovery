/**
 * The self-hosted Protomaps style (docs/contracts/127-m8-basemap.md §2.3): `@protomaps/basemaps`
 * layers for tiles v4 with a Keelson flavour from tokens, every URL under the manifest's own
 * directory (archive via `pmtiles://`, glyphs, sprite), POI and house-number layers removed (calm
 * map; the overlay is the hero). Imported only by the lazy MapCanvas chunk.
 */
import { layers, namedFlavor, type Flavor } from "@protomaps/basemaps";
import type { ThemeName } from "@keelson/tokens";
import type { LayerSpecification, StyleSpecification } from "maplibre-gl";
import { basemapAssetUrl, type InstalledBasemap } from "./basemap";
import { basemapPalette } from "./palette";

/**
 * Attribution of the self-hosted tiles (OpenStreetMap data is ODbL: the credit must stay visible).
 * A constant on purpose: MapLibre renders attribution as HTML, so it is never taken from the
 * manifest or the archive's metadata (`pmtilesProtocol.ts` does not even read the latter).
 */
export const PROTOMAPS_ATTRIBUTION =
  '<a href="https://protomaps.com">Protomaps</a> © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

/** Source id in the style (also the `pmtiles://` source). */
export const PROTOMAPS_SOURCE = "protomaps";

/** Layer id prefixes left out of the Keelson style. */
const DROPPED_LAYERS = ["pois", "address"];

/**
 * Protomaps flavour in Keelson token colours. Starts from the named light/dark flavour so a field
 * added by a future `@protomaps/basemaps` release still has a value (the style test then flags it
 * as a non-token colour to map), without its POI and landcover palettes (no POI layer, landuse
 * colours instead of landcover).
 */
export function keelsonFlavor(theme: ThemeName): Flavor {
  const { pois: _pois, landcover: _landcover, ...base } = namedFlavor(theme === "dark" ? "dark" : "light");
  const p = basemapPalette(theme);
  const landuse = {
    park_a: p.landuse,
    park_b: p.landuse,
    hospital: p.landuse,
    industrial: p.landuse,
    school: p.landuse,
    wood_a: p.landuse,
    wood_b: p.landuse,
    pedestrian: p.landuse,
    scrub_a: p.landuse,
    scrub_b: p.landuse,
    glacier: p.landuse,
    sand: p.landuse,
    beach: p.landuse,
    aerodrome: p.landuse,
    zoo: p.landuse,
    military: p.landuse,
  };
  const casing = p.earth;
  return {
    ...base,
    background: p.water,
    earth: p.earth,
    water: p.water,
    ...landuse,
    runway: p.roadMinor,
    pier: p.earth,
    buildings: p.buildings,

    tunnel_other_casing: casing,
    tunnel_minor_casing: casing,
    tunnel_link_casing: casing,
    tunnel_major_casing: casing,
    tunnel_highway_casing: casing,
    tunnel_other: p.roadMinor,
    tunnel_minor: p.roadMinor,
    tunnel_link: p.roadMinor,
    tunnel_major: p.roadMinor,
    tunnel_highway: p.roadMajor,

    minor_service_casing: casing,
    minor_casing: casing,
    link_casing: casing,
    major_casing_late: casing,
    highway_casing_late: casing,
    other: p.roadMinor,
    minor_service: p.roadMinor,
    minor_a: p.roadMinor,
    minor_b: p.roadMinor,
    link: p.roadMajor,
    major_casing_early: casing,
    major: p.roadMajor,
    highway_casing_early: casing,
    highway: p.highway,

    railway: p.rail,
    boundaries: p.boundary,

    bridges_other_casing: casing,
    bridges_minor_casing: casing,
    bridges_link_casing: casing,
    bridges_major_casing: casing,
    bridges_highway_casing: casing,
    bridges_other: p.roadMinor,
    bridges_minor: p.roadMinor,
    bridges_link: p.roadMajor,
    bridges_major: p.roadMajor,
    bridges_highway: p.highway,

    roads_label_minor: p.labelSoft,
    roads_label_minor_halo: p.earth,
    roads_label_major: p.labelSoft,
    roads_label_major_halo: p.earth,
    ocean_label: p.labelSoft,
    subplace_label: p.labelSoft,
    subplace_label_halo: p.earth,
    city_label: p.labelStrong,
    city_label_halo: p.earth,
    state_label: p.labelSoft,
    state_label_halo: p.earth,
    country_label: p.labelStrong,

    address_label: p.labelSoft,
    address_label_halo: p.earth,
  };
}

/** The Keelson Protomaps style for a theme; pure (the same input gives the same JSON). */
export function protomapsStyle(theme: ThemeName, { manifest, baseUrl }: InstalledBasemap): StyleSpecification {
  const all = layers(PROTOMAPS_SOURCE, keelsonFlavor(theme), { lang: "en" }) as LayerSpecification[];
  return {
    version: 8,
    glyphs: basemapAssetUrl(baseUrl, manifest.glyphs),
    sprite: basemapAssetUrl(baseUrl, manifest.sprites[theme]),
    sources: {
      [PROTOMAPS_SOURCE]: {
        type: "vector",
        url: `pmtiles://${basemapAssetUrl(baseUrl, manifest.archive.path)}`,
        attribution: PROTOMAPS_ATTRIBUTION,
      },
    },
    layers: all.filter((l) => !DROPPED_LAYERS.some((prefix) => l.id.startsWith(prefix))),
  };
}
