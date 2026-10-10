#!/usr/bin/env node
/**
 * Builds the committed offline outline `packages/viz/src/map/assets/ne-110m-land.json` from
 * Natural Earth `ne_110m_land` (public domain; URL + sha256 pinned in versions.json): one
 * MultiPolygon, coordinates rounded to 2 decimals, properties dropped, minified (~50 KB). The
 * network map draws it when no detailed base map is reachable (docs/contracts/127-m8-basemap.md).
 *
 *   node scripts/basemap/build-land-fallback.mjs            # download, verify, write
 *   node scripts/basemap/build-land-fallback.mjs --check    # rebuild in memory, compare with the file
 *   node scripts/basemap/build-land-fallback.mjs --input ne_110m_land.geojson   # offline (still sha256-checked)
 *
 * No npm dependencies (Node 24). Run it in a container, e.g.
 *   docker run --rm -v "$PWD:/src" -w /src node:24-slim node scripts/basemap/build-land-fallback.mjs
 */
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { fetchAllowed, reduceLand, sha256Hex } from "./lib.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.join(here, "..", "..");
const OUT = path.join(repo, "packages", "viz", "src", "map", "assets", "ne-110m-land.json");
const pins = JSON.parse(readFileSync(path.join(here, "versions.json"), "utf8")).natural_earth_land;

const args = process.argv.slice(2);
const check = args.includes("--check");
const inputAt = args.indexOf("--input");
const input = inputAt >= 0 ? args[inputAt + 1] : null;

const raw = input ? readFileSync(input) : Buffer.from(await (await fetchAllowed(pins.url)).arrayBuffer());
const digest = sha256Hex(raw);
if (digest !== pins.sha256) {
  console.error(`ne_110m_land sha256 ${digest} does not match the pinned ${pins.sha256}`);
  process.exit(1);
}
const json = `${JSON.stringify(reduceLand(JSON.parse(raw.toString("utf8")), 2))}\n`;

if (check) {
  let current = "";
  try {
    current = readFileSync(OUT, "utf8");
  } catch {
    // missing file = stale
  }
  if (current !== json) {
    console.error(`${path.relative(repo, OUT)} is stale; run: node scripts/basemap/build-land-fallback.mjs`);
    process.exit(1);
  }
  console.log(`${path.relative(repo, OUT)} is current`);
} else {
  writeFileSync(`${OUT}.tmp`, json);
  renameSync(`${OUT}.tmp`, OUT);
  console.log(`wrote ${path.relative(repo, OUT)} (${Buffer.byteLength(json)} bytes)`);
}
