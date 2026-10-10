#!/usr/bin/env node
/**
 * Fills the `basemap` volume with a self-hosted Protomaps base map (docs/contracts/127-m8-basemap.md):
 *
 *   1. picks a Protomaps daily build (tiles v4) from builds.json (`--build latest|YYYYMMDD`)
 *   2. estimates the world extract up to the profile's zoom with `pmtiles extract --dry-run` and
 *      refuses when it (plus ~14 MB of glyphs / sprites) exceeds BASEMAP_MAX_MB or the free space
 *   3. extracts into a hidden staging directory, `pmtiles verify`, reads the header, sha256
 *   4. downloads glyphs (4 Noto Sans stacks), sprites (light / dark, 1x / 2x) and the font licence
 *      from protomaps/basemaps-assets at a pinned commit, each verified against its git blob id
 *      from the pinned tree; writes LICENSES/
 *   5. installs: removes manifest.json, swaps the asset directories in, moves the archive to its
 *      versioned name (v4/keelson-<build>-<profile>.pmtiles), then writes manifest.json LAST
 *      (temp file + rename), so a partial run never leaves a valid manifest behind
 *   6. prunes older archives (`--keep N`) and the staging directory
 *
 * `--dry-run` stops after step 2 (writes nothing); `--check` validates an installed base map
 * (manifest, archive size + sha256, glyphs, sprites, licences) and exits non-zero on a problem.
 *
 * Runs in the `basemap-fetch` container (infra/basemap/Dockerfile: Node 24 + go-pmtiles, non-root,
 * edge network only). Every request goes through the egress proxy (HTTPS_PROXY, NODE_USE_ENV_PROXY)
 * and only to the hosts in lib.mjs ALLOWED_HOSTS; redirects are re-checked.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  archivePath,
  archivesToPrune,
  assertAllowedUrl,
  assetPlan,
  buildDate,
  buildManifest,
  checkBudget,
  extractArgs,
  fetchAllowed,
  FONTS,
  gitBlobSha1,
  LICENSE_FILES,
  manifestProblems,
  mapLimit,
  parseArgs,
  parseDryRun,
  PROFILES,
  rawUrl,
  resolveBuild,
  resolveLink,
  SPRITE_THEMES,
  USAGE,
  UsageError,
  writeFileAtomic,
} from "./lib.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const PMTILES = process.env.PMTILES_BIN || "pmtiles";
const UA = { "User-Agent": "keelson-basemap-fetch" };
const STALE_STAGING_MS = 6 * 3600 * 1000;

const log = (msg) => console.log(`basemap: ${msg}`);
const mb = (n) => `${(n / 1e6).toFixed(1)} MB`;

/** Runs go-pmtiles; resolves with its output (it logs to stderr), rejects on a non-zero exit. */
function pmtiles(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(PMTILES, args, { stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    let err = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve({ stdout: out, stderr: err });
      else reject(new Error(`pmtiles ${args[0]} exited with ${code}: ${err.trim().split("\n").slice(-3).join(" | ")}`));
    });
  });
}

async function fileSha256(file) {
  const hash = createHash("sha256");
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest("hex");
}

async function getJson(url) {
  const res = await fetchAllowed(url, { headers: { ...UA, Accept: "application/json" }, signal: AbortSignal.timeout(60_000) });
  return res.json();
}

async function getBytes(url, attempts = 3) {
  for (let i = 1; ; i++) {
    try {
      const res = await fetchAllowed(url, { headers: UA, signal: AbortSignal.timeout(60_000) });
      return Buffer.from(await res.arrayBuffer());
    } catch (err) {
      if (i >= attempts) throw err;
      await new Promise((r) => setTimeout(r, 500 * i));
    }
  }
}

function freeBytes(dir) {
  const s = fs.statfsSync(dir);
  return Number(s.bavail) * Number(s.bsize);
}

/** Removes staging directories of crashed runs (hidden, so Caddy never served them). */
function cleanStaleStaging(out) {
  for (const name of fs.readdirSync(out)) {
    if (!name.startsWith(".staging-")) continue;
    const p = path.join(out, name);
    if (Date.now() - fs.statSync(p).mtimeMs > STALE_STAGING_MS) fs.rmSync(p, { recursive: true, force: true });
  }
}

/** Downloads + verifies every asset into `dir` (fonts/, sprites/, LICENSES/). */
async function fetchAssets(dir, versions) {
  const { repo, commit } = versions.assets;
  const tree = await getJson(`https://api.github.com/repos/${repo}/git/trees/${commit}?recursive=1`);
  const { files, links } = assetPlan(tree);
  let done = 0;
  await mapLimit(files, 8, async (f) => {
    const buf = await getBytes(rawUrl(repo, commit, f.path));
    if (gitBlobSha1(buf) !== f.sha) throw new Error(`${f.path}: content does not match the pinned tree`);
    const dest = path.join(dir, ...f.path.split("/"));
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, buf);
    if (++done % 200 === 0) log(`assets ${done}/${files.length + links.length}`);
  });
  const fileSet = new Set(files.map((f) => f.path));
  // Symlinked glyph ranges (Devanagari → Noto Sans Regular): verify the link, then hard-link the
  // target (no extra space; plain files for Caddy and for a bucket upload).
  await mapLimit(links, 8, async (l) => {
    const target = (await getBytes(rawUrl(repo, commit, l.path))).toString("utf8");
    if (gitBlobSha1(target) !== l.sha) throw new Error(`${l.path}: link does not match the pinned tree`);
    const resolved = resolveLink(l.path, target);
    if (!resolved || !fileSet.has(resolved)) throw new Error(`${l.path}: link target ${target} is not a downloaded glyph range`);
    const dest = path.join(dir, ...l.path.split("/"));
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    try {
      fs.linkSync(path.join(dir, ...resolved.split("/")), dest);
    } catch {
      fs.copyFileSync(path.join(dir, ...resolved.split("/")), dest);
    }
  });
  log(`assets ${files.length + links.length} files verified (${repo}@${commit.slice(0, 12)})`);

  const lic = path.join(dir, "LICENSES");
  fs.mkdirSync(lic, { recursive: true });
  fs.copyFileSync(path.join(here, "licenses", "NOTICE.md"), path.join(lic, "NOTICE.md"));
  fs.copyFileSync(path.join(dir, "fonts", "OFL.txt"), path.join(lic, "OFL.txt"));
  const s = versions.sprite_license;
  const mit = await getBytes(rawUrl(s.repo, s.commit, s.path));
  if (gitBlobSha1(mit) !== s.blob_sha1) throw new Error(`${s.repo}/${s.path}: does not match the pinned blob`);
  fs.writeFileSync(path.join(lic, "sprites-MIT.md"), mit);
  for (const f of fs.readdirSync(lic)) fs.chmodSync(path.join(lic, f), 0o644);
}

/** Replaces `out/<name>` with `staged/<name>` (old copy moved aside into staging, removed later). */
function swapDir(out, staged, name) {
  const live = path.join(out, name);
  if (fs.existsSync(live)) fs.renameSync(live, path.join(staged, `old-${name}`));
  fs.renameSync(path.join(staged, "assets", name), live);
}

async function install(opts, versions) {
  const out = path.resolve(opts.out);
  fs.mkdirSync(out, { recursive: true });
  fs.accessSync(out, fs.constants.W_OK);

  const { build, warning } = resolveBuild(await getJson(versions.builds_index), opts.build);
  if (warning) log(`warning: ${warning}`);
  const sourceUrl = assertAllowedUrl(new URL(build.key, versions.builds_base).href).href;
  log(`build ${buildDate(build)} (tiles ${build.version}), profile ${opts.profile} (world z0-${PROFILES[opts.profile].maxzoom})`);

  const dry = await pmtiles(extractArgs({ sourceUrl, out: path.join(os.tmpdir(), "dry-run.pmtiles"), profile: opts.profile, dryRun: true }));
  const estimate = parseDryRun(`${dry.stdout}\n${dry.stderr}`);
  const needed = checkBudget({ estimateBytes: estimate, maxMb: opts.maxMb, freeBytes: freeBytes(out) });
  log(`estimate: archive ${mb(estimate)}, about ${mb(needed)} with glyphs and sprites (budget ${opts.maxMb} MB)`);
  if (opts.dryRun) {
    log("dry run: nothing written");
    return;
  }

  cleanStaleStaging(out);
  const staged = path.join(out, `.staging-${process.pid}-${Date.now()}`);
  fs.mkdirSync(staged);
  try {
    const tmpArchive = path.join(staged, "archive.pmtiles");
    log(`extracting from ${sourceUrl} …`);
    await pmtiles(extractArgs({ sourceUrl, out: tmpArchive, profile: opts.profile }));
    await pmtiles(["verify", tmpArchive]);
    const header = JSON.parse((await pmtiles(["show", tmpArchive, "--header-json"])).stdout);
    if (header.tile_type !== "mvt") throw new Error(`unexpected tile type ${header.tile_type}`);
    if (header.maxzoom !== PROFILES[opts.profile].maxzoom || header.minzoom !== 0) {
      throw new Error(`extract has zooms ${header.minzoom}-${header.maxzoom}, expected 0-${PROFILES[opts.profile].maxzoom}`);
    }
    const bytes = fs.statSync(tmpArchive).size;
    const sha256 = await fileSha256(tmpArchive);
    log(`archive ${mb(bytes)} verified, sha256 ${sha256.slice(0, 16)}…`);

    await fetchAssets(path.join(staged, "assets"), versions);

    // Install. The manifest goes first (no manifest = "not installed": browsers fall back) and
    // comes back last, once everything it points to is in place.
    const manifestFile = path.join(out, "manifest.json");
    fs.rmSync(manifestFile, { force: true });
    for (const name of ["fonts", "sprites", "LICENSES"]) swapDir(out, staged, name);
    const rel = archivePath(build, opts.profile);
    fs.mkdirSync(path.join(out, path.dirname(rel)), { recursive: true });
    fs.renameSync(tmpArchive, path.join(out, rel));
    const manifest = buildManifest({
      generatedAt: new Date().toISOString(),
      profile: opts.profile,
      build,
      sourceUrl,
      archive: { path: rel, bytes, sha256, minzoom: header.minzoom, maxzoom: header.maxzoom },
      assetsCommit: versions.assets.commit,
    });
    writeFileAtomic(manifestFile, `${JSON.stringify(manifest, null, 2)}\n`, fs);
    log(`installed ${rel} + glyphs + sprites; manifest.json written`);

    const archiveDir = path.join(out, path.dirname(rel));
    for (const old of archivesToPrune(fs.readdirSync(archiveDir), path.basename(rel), opts.keep)) {
      fs.rmSync(path.join(archiveDir, old), { force: true });
      log(`pruned ${path.dirname(rel)}/${old}`);
    }
  } finally {
    fs.rmSync(staged, { recursive: true, force: true });
  }
}

/** `--check`: the installed base map is complete and matches its manifest. */
async function check(opts) {
  const out = path.resolve(opts.out);
  const problems = [];
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(path.join(out, "manifest.json"), "utf8"));
  } catch (err) {
    console.error(`basemap: no readable manifest.json in ${out} (${err.code ?? err.message}); run the fetch first`);
    return 1;
  }
  problems.push(...manifestProblems(manifest));
  if (problems.length === 0) {
    const archive = path.join(out, ...manifest.archive.path.split("/"));
    if (!fs.existsSync(archive)) problems.push(`archive ${manifest.archive.path} is missing`);
    else {
      if (fs.statSync(archive).size !== manifest.archive.bytes) problems.push("archive size differs from the manifest");
      else if ((await fileSha256(archive)) !== manifest.archive.sha256) problems.push("archive sha256 differs from the manifest");
    }
    for (const font of FONTS) {
      const dir = path.join(out, "fonts", font);
      const n = fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => f.endsWith(".pbf")).length : 0;
      if (n !== 256) problems.push(`fonts/${font}: ${n} of 256 glyph ranges`);
    }
    for (const theme of SPRITE_THEMES) {
      for (const suffix of [".json", ".png", "@2x.json", "@2x.png"]) {
        const f = `${manifest.sprites[theme]}${suffix}`;
        if (!fs.existsSync(path.join(out, ...f.split("/")))) problems.push(`${f} is missing`);
      }
    }
    for (const f of LICENSE_FILES) if (!fs.existsSync(path.join(out, ...f.split("/")))) problems.push(`${f} is missing`);
  }
  if (problems.length) {
    for (const p of problems) console.error(`basemap: check: ${p}`);
    return 1;
  }
  log(`check ok: ${manifest.archive.path} (${manifest.profile}, build ${manifest.source.build}, ${mb(manifest.archive.bytes)})`);
  return 0;
}

async function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2), process.env);
  } catch (err) {
    if (err instanceof UsageError) {
      console.error(`basemap: ${err.message}\n${USAGE}`);
      return 2;
    }
    throw err;
  }
  if (opts.help) {
    console.log(USAGE);
    return 0;
  }
  if (opts.check) return check(opts);
  const versions = JSON.parse(fs.readFileSync(path.join(here, "versions.json"), "utf8"));
  await install(opts, versions);
  return 0;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error(`basemap: failed: ${err.message}`);
    process.exit(1);
  },
);
