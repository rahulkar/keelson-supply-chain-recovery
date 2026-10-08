#!/usr/bin/env node
/**
 * Copies MapLibre GL's web worker into `public/maplibre/` so the map loads it from our own
 * origin (`setWorkerUrl('/maplibre/maplibre-gl-worker.mjs')`): no blob: workers, strict CSP.
 * maplibre-gl 6 ships the worker as an ES module that imports `./maplibre-gl-shared.mjs`, so
 * both files are copied (from the installed version, so they always match the bundled main
 * module). Runs before `next dev` / `next build`; `--check` only verifies the copies are current.
 */
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.join(here, "..");
const outDir = path.join(appRoot, "public", "maplibre");
const require = createRequire(path.join(appRoot, "package.json"));
const pkgPath = require.resolve("maplibre-gl/package.json");
const pkg = JSON.parse(readFileSync(pkgPath, "utf8"));
const dist = path.join(path.dirname(pkgPath), "dist");

export const FILES = ["maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"];

const sha = (file) => createHash("sha256").update(readFileSync(file)).digest("hex");
const check = process.argv.includes("--check");
const stale = [];

if (!check) mkdirSync(outDir, { recursive: true });
for (const name of FILES) {
  const src = path.join(dist, name);
  if (!existsSync(src)) throw new Error(`maplibre-gl ${pkg.version} has no dist/${name}`);
  const dest = path.join(outDir, name);
  if (existsSync(dest) && sha(dest) === sha(src)) continue;
  if (check) stale.push(name);
  else copyFileSync(src, dest);
}

if (check) {
  if (stale.length) {
    console.error(`public/maplibre is stale (${stale.join(", ")}); run: node scripts/copy-maplibre-worker.mjs`);
    process.exit(1);
  }
} else {
  writeFileSync(path.join(outDir, "VERSION"), `${pkg.version}\n`);
  console.log(`maplibre-gl ${pkg.version} worker → public/maplibre/`);
}
