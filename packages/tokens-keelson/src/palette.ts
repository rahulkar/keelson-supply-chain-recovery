/**
 * Single source of truth for Keelson's design tokens (plan §12).
 *
 * `scripts/gen-tokens.mjs` turns this into `src/tokens.css` (OKLCH CSS variables, light +
 * `[data-theme=dark]`, hex fallbacks for browsers without OKLCH) and `src/generated/hex.ts`
 * (sRGB hex for ECharts / deck.gl / canvas). Only erasable TypeScript syntax here: the
 * generator imports this file directly with Node's type stripping.
 */

/** Colour roles. Each becomes `--k-<role>` in CSS and `--color-<role>` in Tailwind. */
export const colorRoles = [
  // surfaces (cool neutrals)
  "bg",
  "surface",
  "surface-2",
  "surface-3",
  "border",
  "border-strong",
  "scrim",
  // text
  "fg",
  "fg-muted",
  "fg-subtle",
  "fg-inverse",
  // accent (teal)
  "accent",
  "accent-hover",
  "accent-fg",
  "accent-soft",
  "accent-soft-fg",
  "focus",
  // status
  "danger",
  "danger-soft",
  "warning",
  "warning-soft",
  "success",
  "success-soft",
  "info",
  "info-soft",
  // thinking sweep (teal → green → amber)
  "sweep-1",
  "sweep-2",
  "sweep-3",
  // provenance (fg on soft background, plus border)
  "prov-fact",
  "prov-fact-soft",
  "prov-inference",
  "prov-inference-soft",
  "prov-assumption",
  "prov-assumption-soft",
  "prov-simulated",
  "prov-simulated-soft",
  "prov-decision",
  "prov-decision-soft",
  "prov-committed",
  "prov-committed-soft",
  "prov-verified",
  "prov-verified-soft",
  // severity (separate scale from provenance)
  "sev-info",
  "sev-low",
  "sev-medium",
  "sev-high",
  "sev-critical",
  // categorical chart series (canvas)
  "chart-1",
  "chart-2",
  "chart-3",
  "chart-4",
  "chart-5",
  "chart-6",
] as const;

export type ColorRole = (typeof colorRoles)[number];
export type ThemeName = "light" | "dark";

/** OKLCH values per theme. Neutrals use hue 255 (cool) with low chroma; accent hue 190 (teal). */
export const palette: Record<ThemeName, Record<ColorRole, string>> = {
  light: {
    bg: "oklch(0.985 0.003 255)",
    surface: "oklch(1 0 0)",
    "surface-2": "oklch(0.963 0.006 255)",
    "surface-3": "oklch(0.935 0.009 255)",
    border: "oklch(0.905 0.01 255)",
    "border-strong": "oklch(0.8 0.014 255)",
    scrim: "oklch(0.2 0.02 255 / 0.4)",
    fg: "oklch(0.22 0.02 255)",
    "fg-muted": "oklch(0.44 0.02 255)",
    "fg-subtle": "oklch(0.52 0.018 255)",
    "fg-inverse": "oklch(0.985 0.003 255)",
    accent: "oklch(0.52 0.095 192)",
    "accent-hover": "oklch(0.46 0.09 192)",
    "accent-fg": "oklch(0.99 0.005 192)",
    "accent-soft": "oklch(0.95 0.03 192)",
    "accent-soft-fg": "oklch(0.38 0.07 192)",
    focus: "oklch(0.58 0.11 192)",
    danger: "oklch(0.52 0.19 25)",
    "danger-soft": "oklch(0.95 0.03 25)",
    warning: "oklch(0.5 0.12 65)",
    "warning-soft": "oklch(0.96 0.045 85)",
    success: "oklch(0.5 0.12 155)",
    "success-soft": "oklch(0.95 0.035 155)",
    info: "oklch(0.5 0.13 250)",
    "info-soft": "oklch(0.95 0.025 250)",
    "sweep-1": "oklch(0.68 0.12 192)",
    "sweep-2": "oklch(0.74 0.15 150)",
    "sweep-3": "oklch(0.8 0.14 78)",
    "prov-fact": "oklch(0.42 0.025 255)",
    "prov-fact-soft": "oklch(0.96 0.006 255)",
    "prov-inference": "oklch(0.47 0.15 295)",
    "prov-inference-soft": "oklch(0.96 0.025 295)",
    "prov-assumption": "oklch(0.48 0.11 65)",
    "prov-assumption-soft": "oklch(0.965 0.04 85)",
    "prov-simulated": "oklch(0.47 0.13 250)",
    "prov-simulated-soft": "oklch(0.96 0.022 250)",
    "prov-decision": "oklch(0.47 0.15 340)",
    "prov-decision-soft": "oklch(0.96 0.022 340)",
    "prov-committed": "oklch(0.46 0.11 155)",
    "prov-committed-soft": "oklch(0.96 0.03 155)",
    "prov-verified": "oklch(0.42 0.12 160)",
    "prov-verified-soft": "oklch(0.94 0.05 160)",
    "sev-info": "oklch(0.55 0.12 250)",
    "sev-low": "oklch(0.6 0.12 155)",
    "sev-medium": "oklch(0.72 0.14 80)",
    "sev-high": "oklch(0.64 0.17 50)",
    "sev-critical": "oklch(0.55 0.2 25)",
    "chart-1": "oklch(0.53 0.1 192)",
    "chart-2": "oklch(0.55 0.13 250)",
    "chart-3": "oklch(0.68 0.14 70)",
    "chart-4": "oklch(0.55 0.15 330)",
    "chart-5": "oklch(0.6 0.13 145)",
    "chart-6": "oklch(0.5 0.13 290)",
  },
  dark: {
    bg: "oklch(0.17 0.01 255)",
    surface: "oklch(0.205 0.012 255)",
    "surface-2": "oklch(0.24 0.013 255)",
    "surface-3": "oklch(0.285 0.015 255)",
    border: "oklch(0.32 0.015 255)",
    "border-strong": "oklch(0.42 0.018 255)",
    scrim: "oklch(0.05 0.01 255 / 0.6)",
    fg: "oklch(0.95 0.005 255)",
    "fg-muted": "oklch(0.78 0.012 255)",
    "fg-subtle": "oklch(0.7 0.014 255)",
    "fg-inverse": "oklch(0.2 0.015 255)",
    accent: "oklch(0.78 0.11 190)",
    "accent-hover": "oklch(0.84 0.1 190)",
    "accent-fg": "oklch(0.2 0.04 200)",
    "accent-soft": "oklch(0.3 0.05 192)",
    "accent-soft-fg": "oklch(0.88 0.08 190)",
    focus: "oklch(0.78 0.11 190)",
    danger: "oklch(0.72 0.16 25)",
    "danger-soft": "oklch(0.3 0.07 25)",
    warning: "oklch(0.82 0.13 80)",
    "warning-soft": "oklch(0.31 0.06 75)",
    success: "oklch(0.78 0.13 155)",
    "success-soft": "oklch(0.3 0.05 155)",
    info: "oklch(0.76 0.11 250)",
    "info-soft": "oklch(0.3 0.05 250)",
    "sweep-1": "oklch(0.78 0.11 190)",
    "sweep-2": "oklch(0.82 0.14 150)",
    "sweep-3": "oklch(0.86 0.13 80)",
    "prov-fact": "oklch(0.85 0.015 255)",
    "prov-fact-soft": "oklch(0.27 0.012 255)",
    "prov-inference": "oklch(0.82 0.1 295)",
    "prov-inference-soft": "oklch(0.29 0.05 295)",
    "prov-assumption": "oklch(0.86 0.11 80)",
    "prov-assumption-soft": "oklch(0.3 0.05 75)",
    "prov-simulated": "oklch(0.82 0.09 250)",
    "prov-simulated-soft": "oklch(0.29 0.05 250)",
    "prov-decision": "oklch(0.83 0.1 340)",
    "prov-decision-soft": "oklch(0.29 0.05 340)",
    "prov-committed": "oklch(0.84 0.11 155)",
    "prov-committed-soft": "oklch(0.29 0.05 155)",
    "prov-verified": "oklch(0.87 0.13 160)",
    "prov-verified-soft": "oklch(0.32 0.07 160)",
    "sev-info": "oklch(0.74 0.11 250)",
    "sev-low": "oklch(0.76 0.12 155)",
    "sev-medium": "oklch(0.82 0.13 80)",
    "sev-high": "oklch(0.75 0.15 50)",
    "sev-critical": "oklch(0.7 0.17 25)",
    "chart-1": "oklch(0.76 0.11 190)",
    "chart-2": "oklch(0.74 0.11 250)",
    "chart-3": "oklch(0.82 0.13 75)",
    "chart-4": "oklch(0.75 0.13 330)",
    "chart-5": "oklch(0.78 0.13 145)",
    "chart-6": "oklch(0.72 0.12 290)",
  },
};

/** Radii 4/8/12/16/28/32 (px) + pill (composer, chips). */
export const radii = { xs: 4, sm: 8, md: 12, lg: 16, xl: 28, "2xl": 32, pill: 9999 } as const;

/** Type scale 40/28/20/16/14/12 with line heights (px). */
export const typeScale = {
  display: { size: 40, line: 48, weight: 500, tracking: "-0.02em" },
  title: { size: 28, line: 36, weight: 500, tracking: "-0.01em" },
  heading: { size: 20, line: 28, weight: 600, tracking: "-0.005em" },
  body: { size: 16, line: 26, weight: 400, tracking: "0" },
  ui: { size: 14, line: 20, weight: 400, tracking: "0" },
  caption: { size: 12, line: 16, weight: 400, tracking: "0.005em" },
} as const;

/** 4-pt spacing grid. */
export const spacingUnit = 4;

/** Durations (ms) and easings for CSS transitions. */
export const durations = { instant: 80, fast: 120, base: 200, slow: 320, shimmer: 1800 } as const;
export const easings = {
  standard: "cubic-bezier(0.2, 0, 0, 1)",
  emphasized: "cubic-bezier(0.3, 0, 0, 1)",
  exit: "cubic-bezier(0.3, 0, 1, 1)",
} as const;

/** Elevation (theme-independent alpha shadows). */
export const shadows = {
  sm: "0 1px 2px 0 oklch(0.2 0.02 255 / 0.06)",
  md: "0 2px 8px -2px oklch(0.2 0.02 255 / 0.1), 0 1px 2px 0 oklch(0.2 0.02 255 / 0.06)",
  lg: "0 12px 32px -8px oklch(0.2 0.02 255 / 0.16), 0 2px 6px -2px oklch(0.2 0.02 255 / 0.08)",
  composer: "0 4px 20px -6px oklch(0.2 0.02 255 / 0.14), 0 1px 3px 0 oklch(0.2 0.02 255 / 0.06)",
} as const;

/** Layout constants. */
export const layout = {
  chatMaxWidth: 760,
  canvasWidthPct: 55,
  canvasBreakpoint: 1280,
  headerHeight: 56,
} as const;

/** M3 springs converted for Motion (plan §12): standard 700/47.6, canvas reveal 380/31.2. */
export const springs = {
  standard: { type: "spring", stiffness: 700, damping: 47.6, mass: 1 },
  canvasReveal: { type: "spring", stiffness: 380, damping: 31.2, mass: 1 },
} as const;
