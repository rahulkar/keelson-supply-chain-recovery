import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The token-themed MapLibre controls (src/styles.css, docs/contracts/127-m8-basemap.md §7) must win
 * over maplibre-gl.css regardless of source order: the app imports styles.css globally, while
 * maplibre-gl.css arrives later with the lazy MapCanvas chunk, so an equally specific rule of ours
 * would lose (e.g. the half-transparent white attribution with black links in the dark theme).
 */

// vitest runs in the package directory (pnpm --filter @keelson/viz test)
const OURS = readFileSync(path.join(process.cwd(), "src", "styles.css"), "utf8");
const MAPLIBRE = readFileSync(path.join(process.cwd(), "node_modules", "maplibre-gl", "dist", "maplibre-gl.css"), "utf8");

type Rule = { selectors: string[]; props: string[] };

/** Top-level style rules (no @media / @supports blocks, no comments, no `@source …;` statements). */
function rules(css: string): Rule[] {
  let s = css.replace(/\/\*[\s\S]*?\*\//g, "").replace(/@[a-z-]+[^;{]*;/g, "");
  // drop conditional group rules (one level of nesting is all these stylesheets use)
  s = s.replace(/@[a-z-]+[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "");
  const out: Rule[] = [];
  for (const m of s.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = m[1]!.split(",").map(normalize).filter(Boolean);
    const props = m[2]!
      .split(";")
      .map((d) => d.split(":")[0]!.trim())
      .filter(Boolean);
    out.push({ selectors, props });
  }
  return out;
}

const normalize = (sel: string) => sel.trim().replace(/\s*([>+~])\s*/g, " $1 ").replace(/\s+/g, " ");

/** CSS specificity [ids, classes / attributes / pseudo-classes, elements / pseudo-elements]. */
function specificity(selector: string): [number, number, number] {
  let a = 0;
  let b = 0;
  let c = 0;
  let s = selector.replace(/:(not|is|has)\(([^()]*)\)/g, (_m, _fn, arg: string) => {
    const [x, y, z] = specificity(arg);
    a += x;
    b += y;
    c += z;
    return "";
  });
  s = s.replace(/\[[^\]]*\]/g, () => (b++, ""));
  s = s.replace(/#[\w-]+/g, () => (a++, ""));
  s = s.replace(/\.[\w-]+/g, () => (b++, ""));
  s = s.replace(/::[\w-]+/g, () => (c++, ""));
  s = s.replace(/:[\w-]+/g, () => (b++, ""));
  s.replace(/[a-zA-Z][\w-]*/g, () => (c++, ""));
  return [a, b, c];
}

const higher = (x: [number, number, number], y: [number, number, number]) =>
  x[0] !== y[0] ? x[0] > y[0] : x[1] !== y[1] ? x[1] > y[1] : x[2] > y[2];

/** `background` covers `background-color`, `border-top-color` overrides part of `border-top`. */
const sameFamily = (a: string, b: string) => a === b || a.startsWith(`${b}-`) || b.startsWith(`${a}-`);

function declaring(all: Rule[], selector: string, prop: string): Rule | undefined {
  const sel = normalize(selector);
  return all.find((r) => r.selectors.includes(sel) && r.props.some((p) => sameFamily(p, prop)));
}

// [maplibre-gl.css selector, property it sets, our overriding selector]
const OVERRIDES: [string, string, string][] = [
  [".maplibregl-ctrl.maplibregl-ctrl-attrib", "background-color", "[data-map-canvas] .maplibregl-ctrl.maplibregl-ctrl-attrib"],
  [".maplibregl-ctrl-attrib a", "color", "[data-map-canvas] .maplibregl-ctrl-attrib a"],
  [".maplibregl-ctrl-attrib a", "text-decoration", "[data-map-canvas] .maplibregl-ctrl-attrib a"],
  [".maplibregl-ctrl-group", "background", "[data-map-canvas] .maplibregl-ctrl-group:not(:empty)"],
  [".maplibregl-ctrl-group:not(:empty)", "box-shadow", "[data-map-canvas] .maplibregl-ctrl-group:not(:empty)"],
  [".maplibregl-ctrl-group button+button", "border-top", "[data-map-canvas] .maplibregl-ctrl-group button + button"],
  [".maplibregl-ctrl-group button:focus:focus-visible", "box-shadow", "[data-map-canvas] .maplibregl-ctrl-group button:focus:focus-visible"],
];

describe("MapLibre controls on tokens (styles.css vs maplibre-gl.css)", () => {
  const ours = rules(OURS);
  const maplibre = rules(MAPLIBRE);

  it("computes specificity like the CSS spec", () => {
    expect(specificity(".maplibregl-ctrl.maplibregl-ctrl-attrib")).toEqual([0, 2, 0]);
    expect(specificity(".maplibregl-ctrl-group:not(:empty)")).toEqual([0, 2, 0]);
    expect(specificity(".maplibregl-ctrl-group button + button")).toEqual([0, 1, 2]);
    expect(specificity('[data-theme="dark"] .maplibregl-ctrl-group .maplibregl-ctrl-icon')).toEqual([0, 3, 0]);
    expect(specificity("a::before")).toEqual([0, 0, 2]);
  });

  it.each(OVERRIDES)("%s { %s } loses to %s whatever the load order", (theirs, prop, mine) => {
    const upstream = declaring(maplibre, theirs, prop);
    expect(upstream, `maplibre-gl.css no longer has ${theirs} { ${prop} }: review the overrides`).toBeDefined();
    const override = declaring(ours, mine, prop);
    expect(override, `styles.css has no ${mine} { ${prop} }`).toBeDefined();
    expect(higher(specificity(normalize(mine)), specificity(normalize(theirs))), `${mine} vs ${theirs}`).toBe(true);
  });

  it("scopes every MapLibre rule to the network map region", () => {
    const selectors = ours.flatMap((r) => r.selectors).filter((s) => s.includes(".maplibregl-"));
    expect(selectors.length).toBeGreaterThan(0);
    for (const s of selectors) expect(s, s).toMatch(/^(\[data-theme="dark"\] )?\[data-map-canvas\] /);
  });
});
