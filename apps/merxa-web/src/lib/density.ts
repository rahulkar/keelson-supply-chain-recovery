/** Row density (compact = Merxa default 28 px rows / 32 px headers). Stored in a cookie so SSR matches. */

export const DENSITY_COOKIE = "merxa_density";

export type Density = "compact" | "comfortable";

export const DENSITY_METRICS: Record<Density, { rowHeight: number; headerHeight: number }> = {
  compact: { rowHeight: 28, headerHeight: 32 },
  comfortable: { rowHeight: 34, headerHeight: 38 },
};

export function parseDensity(value: string | null | undefined): Density {
  return value === "comfortable" ? "comfortable" : "compact";
}
