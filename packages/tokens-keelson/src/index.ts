/**
 * @keelson/tokens — design tokens for Keelson (plan §12).
 *
 * CSS: `@keelson/tokens/tokens.css` (variables) and `@keelson/tokens/theme.css` (Tailwind 4 @theme).
 * TS:  `tokens` (OKLCH + hex fallbacks for ECharts / deck.gl), `stageLabels`, `provenanceMeta`, springs.
 */
import { hex } from "./generated/hex";
import {
  colorRoles,
  durations,
  easings,
  layout,
  palette,
  radii,
  shadows,
  spacingUnit,
  springs,
  typeScale,
  type ColorRole,
  type ThemeName,
} from "./palette";

export { colorRoles, durations, easings, hex, layout, palette, radii, shadows, spacingUnit, springs, typeScale };
export type { ColorRole, ThemeName };

/** Everything in one object; `color.<theme>` are sRGB hex (safe for canvas libs), `oklch.<theme>` the source values. */
export const tokens = {
  color: hex,
  oklch: palette,
  radii,
  typeScale,
  spacingUnit,
  durations,
  easings,
  shadows,
  layout,
  springs,
} as const;

/** `var(--k-<role>)` for inline styles. */
export function cssVar(role: ColorRole): `var(--k-${ColorRole})` {
  return `var(--k-${role})`;
}

/** Hex colour for a role in a theme (for ECharts / deck.gl / canvas). */
export function hexColor(role: ColorRole, theme: ThemeName = "light"): string {
  return hex[theme][role];
}

/** Hex → `[r, g, b, a]` 0–255 (deck.gl colour arrays). */
export function rgba(role: ColorRole, theme: ThemeName = "light"): [number, number, number, number] {
  const h = hex[theme][role].slice(1);
  const n = (i: number) => parseInt(h.slice(i, i + 2), 16);
  return [n(0), n(2), n(4), h.length === 8 ? n(6) : 255];
}

/** Categorical series colours for charts (hex). */
export function chartPalette(theme: ThemeName = "light"): string[] {
  return (["chart-1", "chart-2", "chart-3", "chart-4", "chart-5", "chart-6"] as const).map((r) => hex[theme][r]);
}

// ───────────────────────────── stages ───────────────────────────────────

/** The 9 fixed stages (`metadata.sc.stage`, `STEP_*.stepName`), in workflow order. */
export const stageOrder = [
  "understanding",
  "checking_systems",
  "measuring_impact",
  "building_options",
  "ready_for_review",
  "preparing_changes",
  "awaiting_approval",
  "applying",
  "verified",
] as const;
export type StageId = (typeof stageOrder)[number];

/** User-facing stage labels (`30-engine-api-events.md`). */
export const stageLabels: Record<StageId, string> = {
  understanding: "Understanding",
  checking_systems: "Checking your systems",
  measuring_impact: "Measuring impact",
  building_options: "Building options",
  ready_for_review: "Ready for review",
  preparing_changes: "Preparing changes",
  awaiting_approval: "Awaiting approval",
  applying: "Applying",
  verified: "Verified",
};

/** Investigation vs remediation phases (plan §4: ≤ 5 stages per phase). */
export const stagePhases: Record<"investigation" | "remediation", readonly StageId[]> = {
  investigation: stageOrder.slice(0, 5),
  remediation: stageOrder.slice(5),
};

export function stagePhase(stage: StageId): "investigation" | "remediation" {
  return stagePhases.investigation.includes(stage) ? "investigation" : "remediation";
}

// ───────────────────────────── provenance ───────────────────────────────

/** Provenance kinds (contract) plus the UI-only `verified` (= committed + read-back). */
export type ProvenanceKind = "fact" | "inference" | "assumption" | "simulated" | "decision" | "committed" | "verified";

export type ProvenanceBorder = "solid" | "dashed" | "dotted" | "hatched" | "none" | "double";

/** Provenance chip spec: colour role, border style and icon name (Lucide) per plan §12. */
export const provenanceMeta: Record<
  ProvenanceKind,
  { label: string; description: string; color: ColorRole; soft: ColorRole; border: ProvenanceBorder; icon: string }
> = {
  fact: {
    label: "Fact",
    description: "Read from a connected system",
    color: "prov-fact",
    soft: "prov-fact-soft",
    border: "solid",
    icon: "database",
  },
  inference: {
    label: "Inferred",
    description: "Inferred from evidence",
    color: "prov-inference",
    soft: "prov-inference-soft",
    border: "dashed",
    icon: "lightbulb",
  },
  assumption: {
    label: "Assumption",
    description: "Assumed or based on priors",
    color: "prov-assumption",
    soft: "prov-assumption-soft",
    border: "dotted",
    icon: "sliders-horizontal",
  },
  simulated: {
    label: "Simulated",
    description: "Computed by a simulation",
    color: "prov-simulated",
    soft: "prov-simulated-soft",
    border: "hatched",
    icon: "flask-conical",
  },
  decision: {
    label: "Decision",
    description: "Stated or decided by a person",
    color: "prov-decision",
    soft: "prov-decision-soft",
    border: "none",
    icon: "user",
  },
  committed: {
    label: "Committed",
    description: "Written to a connected system",
    color: "prov-committed",
    soft: "prov-committed-soft",
    border: "double",
    icon: "check",
  },
  verified: {
    label: "Verified",
    description: "Written and read back from the connected system",
    color: "prov-verified",
    soft: "prov-verified-soft",
    border: "double",
    icon: "check-check",
  },
};

// ───────────────────────────── severity ─────────────────────────────────

export type Severity = "info" | "low" | "medium" | "high" | "critical";

export const severityMeta: Record<Severity, { label: string; color: ColorRole }> = {
  info: { label: "Info", color: "sev-info" },
  low: { label: "Low", color: "sev-low" },
  medium: { label: "Medium", color: "sev-medium" },
  high: { label: "High", color: "sev-high" },
  critical: { label: "Critical", color: "sev-critical" },
};
