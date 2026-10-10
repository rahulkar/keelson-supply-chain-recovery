/**
 * Pure helpers of the self-hosted base map tools (docs/contracts/127-m8-basemap.md):
 * `fetch-basemap.mjs` (Protomaps archive + glyphs + sprites into the `basemap` volume) and
 * `build-land-fallback.mjs` (the committed Natural Earth outline). No npm dependencies; every
 * function here is deterministic and covered by `tests/lib.test.mjs` (node --test).
 */
import { createHash } from "node:crypto";
import path from "node:path";

/** Manifest schema id; the browser (`@keelson/viz` `basemapManifestSchema`) accepts only this. */
export const MANIFEST_SCHEMA = "keelson.basemap/1";

/**
 * Protomaps tiles major version the bundled styles (`@protomaps/basemaps` 5.x) target. A build of
 * another major is refused here and by the browser (the map then falls back).
 */
export const TILES_MAJOR = 4;

/**
 * Extract profiles: whole-world archives up to a max zoom (sizes measured on the 2026-10 builds;
 * MapLibre over-zooms the last level, the network map is capped at maxzoom + 5). Uniform archives
 * only: a sparse "detail where we source" archive would leave blank areas when a user lands at a
 * deep zoom outside the detail regions (no parent tile in MapLibre's cache to over-zoom).
 */
export const PROFILES = Object.freeze({
  lite: Object.freeze({ maxzoom: 5, approxMb: 15 }),
  standard: Object.freeze({ maxzoom: 6, approxMb: 45 }),
  detail: Object.freeze({ maxzoom: 7, approxMb: 189 }),
});
export const DEFAULT_PROFILE = "standard";
export const DEFAULT_MAX_MB = 300;
/** Glyphs (4 font stacks) + sprites + licences, rounded up; counted against the budget. */
export const ASSETS_BUDGET_BYTES = 14 * 1_000_000;

/** Every host the fetch tool may contact (through the egress proxy). Anything else is refused. */
export const ALLOWED_HOSTS = Object.freeze([
  "build.protomaps.com",
  "build-metadata.protomaps.dev",
  "api.github.com",
  "raw.githubusercontent.com",
]);

/** Font stacks the Protomaps styles reference (Devanagari is hard-coded for Indian place names). */
export const FONTS = Object.freeze(["Noto Sans Regular", "Noto Sans Medium", "Noto Sans Italic", "Noto Sans Devanagari Regular v1"]);
export const SPRITE_THEMES = Object.freeze(["light", "dark"]);
export const GLYPHS_TEMPLATE = "fonts/{fontstack}/{range}.pbf";
export const LICENSE_FILES = Object.freeze(["LICENSES/NOTICE.md", "LICENSES/OFL.txt", "LICENSES/sprites-MIT.md"]);

/**
 * A relative asset path: no scheme, no leading slash, no `..`/`.` segment, no backslash, no `{`/`}`
 * (URL template placeholders; the glyphs template is checked as a literal). Same rule as the
 * browser's `basemapManifestSchema` paths.
 */
const SAFE_PATH = /^[A-Za-z0-9 ._@-]+(?:\/[A-Za-z0-9 ._@-]+)*$/;
export function isSafeRelativePath(p) {
  return typeof p === "string" && p.length <= 200 && SAFE_PATH.test(p) && !p.split("/").some((s) => s === "." || s === "..");
}

export class UsageError extends Error {
  constructor(message) {
    super(message);
    this.name = "UsageError";
  }
}

const HELP = `Usage: fetch-basemap [--profile lite|standard|detail] [--build latest|YYYYMMDD] [--max-mb N]
                     [--keep N] [--dry-run | --check] [--out DIR]
Env defaults: BASEMAP_PROFILE (standard), BASEMAP_BUILD (latest), BASEMAP_MAX_MB (300), BASEMAP_DIR (/srv/basemap).`;
export const USAGE = HELP;

/** CLI flags over env defaults. Throws UsageError on anything unexpected. */
export function parseArgs(argv, env = {}) {
  const opts = {
    profile: env.BASEMAP_PROFILE || DEFAULT_PROFILE,
    build: env.BASEMAP_BUILD || "latest",
    maxMb: env.BASEMAP_MAX_MB ? Number(env.BASEMAP_MAX_MB) : DEFAULT_MAX_MB,
    keep: 1,
    dryRun: false,
    check: false,
    help: false,
    out: env.BASEMAP_DIR || "/srv/basemap",
  };
  const takes = new Set(["--profile", "--build", "--max-mb", "--keep", "--out"]);
  for (let i = 0; i < argv.length; i++) {
    let arg = argv[i];
    let value;
    const eq = arg.indexOf("=");
    if (arg.startsWith("--") && eq > 0) {
      value = arg.slice(eq + 1);
      arg = arg.slice(0, eq);
    }
    if (takes.has(arg) && value === undefined) {
      value = argv[++i];
      if (value === undefined) throw new UsageError(`${arg} needs a value`);
    }
    switch (arg) {
      case "--profile":
        opts.profile = value;
        break;
      case "--build":
        opts.build = value;
        break;
      case "--max-mb":
        opts.maxMb = Number(value);
        break;
      case "--keep":
        opts.keep = Number(value);
        break;
      case "--out":
        opts.out = value;
        break;
      case "--dry-run":
        opts.dryRun = true;
        break;
      case "--check":
        opts.check = true;
        break;
      case "-h":
      case "--help":
        opts.help = true;
        break;
      default:
        throw new UsageError(`unknown argument ${arg}`);
    }
  }
  if (!Object.hasOwn(PROFILES, opts.profile)) throw new UsageError(`unknown profile ${opts.profile} (lite, standard, detail)`);
  if (opts.build !== "latest" && !/^\d{8}$/.test(opts.build)) throw new UsageError(`--build must be latest or YYYYMMDD, got ${opts.build}`);
  if (!Number.isInteger(opts.maxMb) || opts.maxMb < 1 || opts.maxMb > 100_000) throw new UsageError("--max-mb must be a whole number of MB");
  if (!Number.isInteger(opts.keep) || opts.keep < 1 || opts.keep > 10) throw new UsageError("--keep must be 1-10");
  if (opts.dryRun && opts.check) throw new UsageError("--dry-run and --check are exclusive");
  return opts;
}

/** `4.15.2` → 4 (NaN for anything that is not a semver-ish string). */
export function majorOf(version) {
  const m = /^(\d+)\.\d+\.\d+$/.exec(String(version ?? ""));
  return m ? Number(m[1]) : Number.NaN;
}

/**
 * Picks the Protomaps daily build from `builds.json` (`[{key: "20261009.pmtiles", size, version,
 * b3sum?, uploaded}]`). `latest` = newest build of the supported tiles major. A pinned build that
 * has aged out of the index (daily builds are kept for about two months) falls back to latest with
 * a warning; a pinned build of another tiles major is an error.
 */
export function resolveBuild(builds, requested, tilesMajor = TILES_MAJOR) {
  if (!Array.isArray(builds)) throw new Error("builds index is not a list");
  const valid = builds.filter(
    (b) => b && typeof b.key === "string" && /^\d{8}\.pmtiles$/.test(b.key) && Number.isSafeInteger(b.size) && b.size > 0,
  );
  const supported = valid.filter((b) => majorOf(b.version) === tilesMajor).sort((a, b) => (a.key < b.key ? -1 : 1));
  const latest = supported.at(-1);
  if (requested === "latest") {
    if (!latest) throw new Error(`no Protomaps build with tiles v${tilesMajor} in the index`);
    return { build: latest, warning: null };
  }
  const pinned = valid.find((b) => b.key === `${requested}.pmtiles`);
  if (pinned) {
    if (majorOf(pinned.version) !== tilesMajor) {
      throw new Error(`build ${requested} has tiles v${pinned.version}; the bundled styles need v${tilesMajor}.x`);
    }
    return { build: pinned, warning: null };
  }
  if (!latest) throw new Error(`build ${requested} is not in the index and no tiles v${tilesMajor} build is`);
  return { build: latest, warning: `build ${requested} is no longer published; using the latest build ${buildDate(latest)}` };
}

/** `20261009.pmtiles` → `20261009`. */
export function buildDate(build) {
  return build.key.replace(/\.pmtiles$/, "");
}

/** Immutable, versioned archive path inside the volume. */
export function archivePath(build, profile) {
  return `v${TILES_MAJOR}/keelson-${buildDate(build)}-${profile}.pmtiles`;
}

/** go-pmtiles argv for a world extract up to the profile's max zoom. */
export function extractArgs({ sourceUrl, out, profile, dryRun = false }) {
  const p = PROFILES[profile];
  if (!p) throw new UsageError(`unknown profile ${profile}`);
  return ["extract", sourceUrl, out, "--minzoom=0", `--maxzoom=${p.maxzoom}`, "--download-threads=4", ...(dryRun ? ["--dry-run"] : [])];
}

const UNITS = { B: 1, kB: 1e3, KB: 1e3, MB: 1e6, GB: 1e9, TB: 1e12, KiB: 1024, MiB: 1024 ** 2, GiB: 1024 ** 3, TiB: 1024 ** 4 };

/** Archive size estimate in bytes from `pmtiles extract --dry-run` output (its log goes to stderr). */
export function parseDryRun(output) {
  const m = /for an archive size of ([\d.]+) ?(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\b/.exec(String(output));
  if (!m) throw new Error("could not read the archive size from the pmtiles dry run");
  return Math.round(Number(m[1]) * UNITS[m[2]]);
}

/** Throws when the extract (plus assets) would not fit the budget or the volume. */
export function checkBudget({ estimateBytes, maxMb, freeBytes = Number.POSITIVE_INFINITY, assetsBytes = ASSETS_BUDGET_BYTES }) {
  const needed = estimateBytes + assetsBytes;
  const mb = (n) => `${Math.ceil(n / 1e6)} MB`;
  if (needed > maxMb * 1e6) {
    throw new Error(`the extract needs about ${mb(needed)} (archive ${mb(estimateBytes)} + assets) but BASEMAP_MAX_MB is ${maxMb} MB`);
  }
  // twice the archive: the new one is written next to the one in use until it is verified
  if (needed + estimateBytes > freeBytes) {
    throw new Error(`the basemap volume has ${mb(freeBytes)} free; about ${mb(needed + estimateBytes)} are needed`);
  }
  return needed;
}

/** Git blob id (SHA-1 of `blob <len>\0<content>`), as listed by the GitHub tree API. */
export function gitBlobSha1(buf) {
  const data = Buffer.isBuffer(buf) ? buf : Buffer.from(buf);
  return createHash("sha1").update(`blob ${data.length}\0`).update(data).digest("hex");
}

export function sha256Hex(buf) {
  return createHash("sha256").update(buf).digest("hex");
}

/** Host allowlist check (https only). Returns the parsed URL. */
export function assertAllowedUrl(url, allowed = ALLOWED_HOSTS) {
  let u;
  try {
    u = new URL(url);
  } catch {
    throw new Error(`invalid URL: ${url}`);
  }
  if (u.protocol !== "https:" || u.username || u.password || (u.port && u.port !== "443") || !allowed.includes(u.hostname)) {
    throw new Error(`refusing to fetch ${u.protocol}//${u.host}: not an allowed source`);
  }
  return u;
}

/**
 * `fetch` limited to the allowlist: redirects are followed by hand (at most 3) and every target is
 * checked again, so a redirect can never lead to another host.
 */
export async function fetchAllowed(url, { fetchImpl = fetch, allowed = ALLOWED_HOSTS, maxRedirects = 3, headers = {}, signal } = {}) {
  let current = assertAllowedUrl(url, allowed).href;
  for (let hop = 0; ; hop++) {
    const res = await fetchImpl(current, { redirect: "manual", headers, signal });
    if (res.status >= 300 && res.status < 400) {
      const location = res.headers.get("location");
      if (!location || hop >= maxRedirects) throw new Error(`too many or empty redirects from ${current}`);
      current = assertAllowedUrl(new URL(location, current).href, allowed).href;
      continue;
    }
    if (!res.ok) throw new Error(`GET ${current} answered ${res.status}`);
    return res;
  }
}

/** `raw.githubusercontent.com` URL of a file at a commit (path segments percent-encoded). */
export function rawUrl(repo, commit, filePath) {
  return `https://raw.githubusercontent.com/${repo}/${commit}/${filePath.split("/").map(encodeURIComponent).join("/")}`;
}

/** Resolves a symlink target (relative, posix) from the link's own directory; null if it escapes `root`. */
export function resolveLink(linkPath, target, root = "fonts") {
  if (typeof target !== "string" || target.startsWith("/") || target.includes("\\")) return null;
  const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(linkPath), target));
  return resolved.startsWith(`${root}/`) && isSafeRelativePath(resolved) ? resolved : null;
}

/**
 * Which files to take from the pinned basemaps-assets tree (GitHub `git/trees/<sha>?recursive=1`):
 * every glyph range of `fonts`, the light/dark sprites at 1x and 2x, and the font licence. Glyph
 * entries may be symlinks (mode 120000, the Devanagari stack links unchanged ranges to Noto Sans
 * Regular); they are returned separately with the file they point to.
 */
export function assetPlan(tree, { fonts = FONTS, sprites = SPRITE_THEMES } = {}) {
  if (!tree || !Array.isArray(tree.tree) || tree.truncated) throw new Error("the assets tree listing is missing or truncated");
  const blobs = new Map(tree.tree.filter((t) => t.type === "blob").map((t) => [t.path, t]));
  const files = [];
  const links = [];
  for (const font of fonts) {
    const prefix = `fonts/${font}/`;
    const entries = [...blobs.values()].filter((t) => t.path.startsWith(prefix) && /^\d+-\d+\.pbf$/.test(t.path.slice(prefix.length)));
    if (entries.length !== 256) throw new Error(`font ${font}: expected 256 glyph ranges, found ${entries.length}`);
    for (const t of entries) (t.mode === "120000" ? links : files).push({ path: t.path, sha: t.sha, size: t.size ?? 0 });
  }
  const wanted = [...sprites.flatMap((s) => [`sprites/v4/${s}.json`, `sprites/v4/${s}.png`, `sprites/v4/${s}@2x.json`, `sprites/v4/${s}@2x.png`]), "fonts/OFL.txt"];
  for (const p of wanted) {
    const t = blobs.get(p);
    if (!t || t.mode === "120000") throw new Error(`assets tree has no file ${p}`);
    files.push({ path: p, sha: t.sha, size: t.size ?? 0 });
  }
  for (const f of [...files, ...links]) if (!isSafeRelativePath(f.path)) throw new Error(`unsafe asset path ${f.path}`);
  files.sort((a, b) => (a.path < b.path ? -1 : 1));
  links.sort((a, b) => (a.path < b.path ? -1 : 1));
  return { files, links };
}

/** Run `fn` over `items` with at most `limit` in flight; rejects on the first failure. */
export async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      results[i] = await fn(items[i], i);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return results;
}

/**
 * The `keelson.basemap/1` manifest (written last, via temp file + rename). Paths are relative to
 * the manifest's directory; the browser builds every URL itself and never takes attribution or
 * other text from here. Key order is fixed (the fixture in packages/viz/tests/fixtures matches it).
 */
export function buildManifest({ generatedAt, profile, build, sourceUrl, archive, assetsCommit }) {
  return {
    schema: MANIFEST_SCHEMA,
    generated_at: generatedAt,
    profile,
    source: {
      build: buildDate(build),
      basemap_version: build.version,
      url: sourceUrl,
      b3sum: typeof build.b3sum === "string" && /^[0-9a-f]{64}$/.test(build.b3sum) ? build.b3sum : null,
    },
    archive: {
      path: archive.path,
      bytes: archive.bytes,
      sha256: archive.sha256,
      minzoom: archive.minzoom,
      maxzoom: archive.maxzoom,
    },
    glyphs: GLYPHS_TEMPLATE,
    fonts: [...FONTS],
    sprites: Object.fromEntries(SPRITE_THEMES.map((t) => [t, `sprites/v4/${t}`])),
    assets_commit: assetsCommit,
    licenses: [...LICENSE_FILES],
  };
}

/**
 * Structural check of a manifest read back from the volume (`--check`); the browser's zod schema
 * is the authority, this catches a broken install early. Returns a list of problems.
 */
export function manifestProblems(m) {
  const problems = [];
  const need = (cond, msg) => cond || problems.push(msg);
  need(m && typeof m === "object" && !Array.isArray(m), "manifest is not an object");
  if (problems.length) return problems;
  need(m.schema === MANIFEST_SCHEMA, `schema is not ${MANIFEST_SCHEMA}`);
  need(majorOf(m.source?.basemap_version) === TILES_MAJOR, `tiles version is not ${TILES_MAJOR}.x`);
  need(isSafeRelativePath(m.archive?.path) && m.archive.path.endsWith(".pmtiles"), "archive.path is not a safe relative .pmtiles path");
  need(/^[0-9a-f]{64}$/.test(m.archive?.sha256 ?? ""), "archive.sha256 is not a sha256");
  need(Number.isSafeInteger(m.archive?.bytes) && m.archive.bytes > 0, "archive.bytes is not a positive integer");
  need(m.glyphs === GLYPHS_TEMPLATE, `glyphs is not ${GLYPHS_TEMPLATE}`);
  need(Array.isArray(m.fonts) && m.fonts.every((f) => isSafeRelativePath(f)), "fonts are not safe names");
  need(SPRITE_THEMES.every((t) => isSafeRelativePath(m.sprites?.[t])), "sprites are not safe relative paths");
  return problems;
}

/**
 * Writes `file` via a hidden temp file in the same directory + rename, so readers (Caddy, a
 * browser) see the old content or the complete new one, never a partial file. `fsImpl` is
 * `node:fs` (injectable for tests).
 */
export function writeFileAtomic(file, data, fsImpl) {
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.tmp`);
  fsImpl.writeFileSync(tmp, data);
  try {
    fsImpl.renameSync(tmp, file);
  } catch (err) {
    fsImpl.rmSync(tmp, { force: true });
    throw err;
  }
}

/** Archives to delete after a successful install: older `v4/keelson-*.pmtiles`, keeping `keep` newest (incl. current). */
export function archivesToPrune(names, current, keep = 1) {
  const ours = names.filter((n) => /^keelson-\d{8}-(lite|standard|detail)\.pmtiles$/.test(n) && n !== current);
  ours.sort().reverse();
  return ours.slice(Math.max(0, keep - 1));
}

// ───────────────────────────── Natural Earth outline ─────────────────────────────

const round = (v, f) => {
  const r = Math.round(v * f) / f;
  return Object.is(r, -0) ? 0 : r;
};

function reduceRing(ring, f) {
  const out = [];
  for (const [x, y] of ring) {
    const p = [round(x, f), round(y, f)];
    const last = out.at(-1);
    if (!last || last[0] !== p[0] || last[1] !== p[1]) out.push(p);
  }
  if (out.length && (out[0][0] !== out.at(-1)[0] || out[0][1] !== out.at(-1)[1])) out.push([...out[0]]);
  return out.length >= 4 ? out : null;
}

/**
 * Natural Earth `ne_110m_land` → one MultiPolygon feature: coordinates rounded to `decimals`,
 * repeated points dropped, rings closed, degenerate rings / polygons removed, properties dropped.
 */
export function reduceLand(collection, decimals = 2) {
  if (collection?.type !== "FeatureCollection" || !Array.isArray(collection.features)) throw new Error("not a GeoJSON FeatureCollection");
  const f = 10 ** decimals;
  const polygons = [];
  for (const feature of collection.features) {
    const g = feature?.geometry;
    const parts = g?.type === "Polygon" ? [g.coordinates] : g?.type === "MultiPolygon" ? g.coordinates : [];
    for (const poly of parts) {
      const outer = reduceRing(poly[0] ?? [], f);
      if (!outer) continue;
      const holes = poly.slice(1).map((r) => reduceRing(r, f)).filter(Boolean);
      polygons.push([outer, ...holes]);
    }
  }
  return {
    type: "FeatureCollection",
    features: [{ type: "Feature", properties: {}, geometry: { type: "MultiPolygon", coordinates: polygons } }],
  };
}
