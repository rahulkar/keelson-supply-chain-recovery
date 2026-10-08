// @vitest-environment node
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

/** Merxa is a separate product: the lint config must reject any Keelson import. */
describe("ESLint product boundary", () => {
  const cwd = process.cwd(); // vitest runs from the app root

  async function ruleIds(code: string): Promise<(string | null)[]> {
    const eslint = new ESLint({ cwd });
    const [result] = await eslint.lintText(code, { filePath: "src/lib/__boundary_probe__.ts" });
    return result?.messages.map((m) => m.ruleId) ?? [];
  }

  it.each([
    'import { Button } from "@keelson/ui";\nexport const B = Button;\n',
    'import * as t from "@keelson/tokens";\nexport const T = t;\n',
    'import { x } from "../../../packages/ui-keelson/src/index";\nexport const X = x;\n',
    'export { Card } from "@keelson/ui";\n',
  ])("flags %s", async (code) => {
    expect(await ruleIds(code)).toContain("no-restricted-imports");
  }, 60_000);

  it.each([
    'export const load = () => import("@keelson/viz");\n',
    'export const load = async () => (await import("@keelson")).default;\n',
    'declare const require: (id: string) => unknown;\nexport const t = require("@keelson/tokens");\n',
  ])("flags dynamic loads: %s", async (code) => {
    expect(await ruleIds(code)).toContain("no-restricted-syntax");
  }, 60_000);

  it("allows ordinary imports and look-alike package names", async () => {
    const ids = await ruleIds(
      'import { formatInt } from "@/lib/format";\nexport const f = formatInt;\nexport const g = () => import("@keelsonite/x");\n',
    );
    expect(ids.filter((id) => id === "no-restricted-imports" || id === "no-restricted-syntax")).toEqual([]);
  }, 60_000);

  it("has no Keelson package in package.json and no @keelson reference in source or style files", () => {
    const pkg = JSON.parse(readFileSync(join(cwd, "package.json"), "utf8")) as Record<string, unknown>;
    for (const field of ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]) {
      const deps = (pkg[field] ?? {}) as Record<string, string>;
      expect(Object.keys(deps).filter((name) => name.startsWith("@keelson"))).toEqual([]);
    }
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir)) {
        const path = join(dir, entry);
        if (statSync(path).isDirectory()) {
          walk(path);
        } else if (/\.(tsx?|mjs|cjs|js|css)$/.test(entry) && readFileSync(path, "utf8").includes("@keelson")) {
          offenders.push(path);
        }
      }
    };
    walk(join(cwd, "src"));
    expect(offenders).toEqual([]);
  });
});
