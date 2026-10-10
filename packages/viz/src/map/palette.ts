import { hexColor, type ColorRole, type ThemeName } from "@keelson/tokens";

/**
 * Base map colours from Keelson tokens (both the Protomaps style and the offline outline). Calm on
 * purpose — the overlay is the hero: land and water differ by about 1.1:1, labels stay ≥ 4.5:1 on
 * the colour they sit on, and land / water never lower the overlay's contrast below the M3 plain
 * fallback background (`surface-2`). `tests/protomaps-style.test.ts` holds these rules.
 */
export type BasemapPalette = {
  earth: ColorRole;
  water: ColorRole;
  /** Parks, woods, landuse areas, glaciers. */
  landuse: ColorRole;
  roadMinor: ColorRole;
  roadMajor: ColorRole;
  highway: ColorRole;
  rail: ColorRole;
  boundary: ColorRole;
  buildings: ColorRole;
  /** Country and city names. */
  labelStrong: ColorRole;
  /** Regions, districts, roads, oceans. */
  labelSoft: ColorRole;
};

const ROLES: Record<ThemeName, BasemapPalette> = {
  light: {
    earth: "surface",
    water: "surface-2",
    landuse: "bg",
    roadMinor: "surface-3",
    roadMajor: "border",
    highway: "border-strong",
    rail: "border-strong",
    boundary: "border-strong",
    buildings: "surface-3",
    labelStrong: "fg-muted",
    labelSoft: "fg-subtle",
  },
  dark: {
    earth: "surface-2",
    water: "bg",
    landuse: "surface",
    roadMinor: "surface-3",
    roadMajor: "border",
    highway: "border-strong",
    rail: "border-strong",
    boundary: "border-strong",
    buildings: "surface-3",
    labelStrong: "fg-muted",
    labelSoft: "fg-subtle",
  },
};

/** Token roles of the base map in a theme. */
export function basemapRoles(theme: ThemeName): BasemapPalette {
  return ROLES[theme];
}

/** Hex colours of the base map in a theme (MapLibre paint values). */
export function basemapPalette(theme: ThemeName): Record<keyof BasemapPalette, string> {
  const roles = ROLES[theme];
  return Object.fromEntries(Object.entries(roles).map(([k, role]) => [k, hexColor(role, theme)])) as Record<keyof BasemapPalette, string>;
}
