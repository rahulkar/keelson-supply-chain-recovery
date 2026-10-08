import fs from "node:fs";
import { wcagContrast } from "culori";
import { describe, expect, it } from "vitest";
import { outputs, sameText, toHex } from "../scripts/gen-tokens.mjs";
import {
  chartPalette,
  colorRoles,
  hex,
  palette,
  provenanceMeta,
  rgba,
  stageLabels,
  stageOrder,
  stagePhase,
  tokens,
  type ColorRole,
  type ThemeName,
} from "../src";

describe("generated outputs", () => {
  it("tokens.css and generated/hex.ts are in sync with palette.ts", () => {
    for (const [file, contents] of outputs()) {
      expect(fs.readFileSync(file, "utf8").replace(/\r\n/g, "\n"), `${file} is stale — run pnpm generate`).toBe(contents);
    }
  });

  it("compares generated text independently of line endings (Windows autocrlf checkouts)", () => {
    expect(sameText("a\r\nb\r\n", "a\nb\n")).toBe(true);
    expect(sameText("a\nb\n", "a\nc\n")).toBe(false);
    expect(sameText(null, "a")).toBe(false);
  });

  it("every role has an OKLCH value and a hex fallback in both themes", () => {
    for (const theme of ["light", "dark"] as const) {
      for (const role of colorRoles) {
        expect(palette[theme][role]).toMatch(/^oklch\(/);
        expect(hex[theme][role]).toMatch(/^#[0-9a-f]{6}([0-9a-f]{2})?$/);
      }
    }
  });

  it("converts OKLCH to hex (gamut-mapped)", () => {
    expect(toHex("oklch(1 0 0)")).toBe("#ffffff");
    expect(toHex("oklch(0 0 0)")).toBe("#000000");
    expect(toHex("oklch(0.2 0.02 255 / 0.4)")).toMatch(/^#[0-9a-f]{8}$/);
  });

  it("theme.css maps every colour role into Tailwind", () => {
    const theme = fs.readFileSync(new URL("../src/theme.css", import.meta.url), "utf8");
    for (const role of colorRoles) expect(theme).toContain(`--color-${role}: var(--k-${role});`);
    expect(theme).toContain('--font-sans: "Inter Variable"');
    expect(theme).toContain("@custom-variant dark (&:where([data-theme=dark], [data-theme=dark] *));");
  });
});

describe("WCAG 2.2 AA contrast (4.5:1 text, 3:1 UI)", () => {
  const pairs: [ColorRole, ColorRole, number][] = [
    ["fg", "bg", 4.5],
    ["fg", "surface", 4.5],
    ["fg", "surface-2", 4.5],
    ["fg-muted", "surface", 4.5],
    ["fg-muted", "surface-2", 4.5],
    ["fg-subtle", "surface", 4.5],
    ["accent", "surface", 4.5],
    ["accent-fg", "accent", 4.5],
    ["accent-soft-fg", "accent-soft", 4.5],
    ["danger", "surface", 4.5],
    ["warning", "surface", 4.5],
    ["success", "surface", 4.5],
    ["info", "surface", 4.5],
    ["prov-fact", "prov-fact-soft", 4.5],
    ["prov-inference", "prov-inference-soft", 4.5],
    ["prov-assumption", "prov-assumption-soft", 4.5],
    ["prov-simulated", "prov-simulated-soft", 4.5],
    ["prov-decision", "prov-decision-soft", 4.5],
    ["prov-committed", "prov-committed-soft", 4.5],
    ["prov-verified", "prov-verified-soft", 4.5],
    ["focus", "surface", 3],
    ["border-strong", "surface", 1.5],
    // Pairs used by @keelson/ui and @keelson/viz components:
    ["fg-subtle", "surface-2", 4.5], // captions on tiles / matched records
    ["fg-subtle", "bg", 4.5],
    ["fg-muted", "surface-3", 4.5], // tab badges
    ["warning", "surface-2", 4.5], // "Preliminary" inside KPI tiles
    ["danger", "danger-soft", 4.5], // Badge tones
    ["success", "success-soft", 4.5],
    ["warning", "warning-soft", 4.5],
    ["info", "info-soft", 4.5],
    ["fg-inverse", "fg", 4.5], // tooltips
    ["fg-inverse", "danger", 4.5], // danger button
  ];
  for (const theme of ["light", "dark"] as ThemeName[]) {
    for (const [fg, bg, min] of pairs) {
      it(`${theme}: ${fg} on ${bg} ≥ ${min}`, () => {
        expect(wcagContrast(hex[theme][fg], hex[theme][bg])).toBeGreaterThanOrEqual(min);
      });
    }
  }

  // Avatar initials (@keelson/ui Avatar TONES): text role per chart background and theme.
  const avatarTones: Record<ThemeName, [ColorRole, ColorRole][]> = {
    light: [
      ["fg-inverse", "chart-1"],
      ["fg-inverse", "chart-2"],
      ["fg", "chart-3"],
      ["fg-inverse", "chart-4"],
      ["fg", "chart-5"],
      ["fg-inverse", "chart-6"],
    ],
    // In dark mode fg-inverse is near-black and every chart colour is light.
    dark: (["chart-1", "chart-2", "chart-3", "chart-4", "chart-5", "chart-6"] as const).map((c) => ["fg-inverse", c]),
  };
  for (const theme of ["light", "dark"] as ThemeName[]) {
    for (const [fg, bg] of avatarTones[theme]) {
      it(`${theme}: avatar ${fg} on ${bg} ≥ 4.5`, () => {
        expect(wcagContrast(hex[theme][fg], hex[theme][bg])).toBeGreaterThanOrEqual(4.5);
      });
    }
  }
});

describe("ts exports", () => {
  it("has labels for all 9 stages in order", () => {
    expect(stageOrder).toHaveLength(9);
    expect(stageOrder.map((s) => stageLabels[s])).toEqual([
      "Understanding",
      "Checking your systems",
      "Measuring impact",
      "Building options",
      "Ready for review",
      "Preparing changes",
      "Awaiting approval",
      "Applying",
      "Verified",
    ]);
    expect(stagePhase("ready_for_review")).toBe("investigation");
    expect(stagePhase("applying")).toBe("remediation");
  });

  it("provenance meta follows plan §12 (border + icon per kind)", () => {
    expect(provenanceMeta.fact).toMatchObject({ border: "solid", icon: "database" });
    expect(provenanceMeta.inference).toMatchObject({ border: "dashed", icon: "lightbulb" });
    expect(provenanceMeta.assumption).toMatchObject({ border: "dotted", icon: "sliders-horizontal" });
    expect(provenanceMeta.simulated).toMatchObject({ border: "hatched", icon: "flask-conical" });
    expect(provenanceMeta.decision).toMatchObject({ border: "none", icon: "user" });
    expect(provenanceMeta.committed).toMatchObject({ border: "double", icon: "check" });
    expect(provenanceMeta.verified).toMatchObject({ border: "double", icon: "check-check" });
  });

  it("exposes canvas helpers", () => {
    expect(chartPalette("dark")).toHaveLength(6);
    expect(rgba("surface", "light")).toEqual([255, 255, 255, 255]);
    expect(rgba("scrim", "light")[3]).toBeLessThan(255);
    expect(tokens.springs.standard).toMatchObject({ stiffness: 700, damping: 47.6 });
    expect(tokens.springs.canvasReveal).toMatchObject({ stiffness: 380, damping: 31.2 });
    expect(Object.values(tokens.radii)).toEqual([4, 8, 12, 16, 28, 32, 9999]);
    expect(tokens.layout.chatMaxWidth).toBe(760);
  });
});
