/**
 * Base map selection for the network map (docs/contracts/127-m8-basemap.md): the runtime mode, the
 * self-hosted Protomaps manifest and the fallback chain. Light on purpose (zod + fetch only): it
 * runs before the lazy MapCanvas chunk; the styles themselves are built inside that chunk.
 */
import { z } from "zod";

/** Runtime switch (`KEELSON_BASEMAP` in the web app). */
export const BASEMAP_MODES = ["auto", "protomaps", "openfreemap", "offline"] as const;
export type BasemapMode = (typeof BASEMAP_MODES)[number];

/**
 * What is drawn under the overlay: self-hosted Protomaps vector tiles, OpenFreeMap's hosted styles,
 * the committed Natural Earth outline (no network at all), or a plain token background (last
 * resort, needs nothing but the canvas).
 */
export type BasemapKind = "protomaps" | "openfreemap" | "offline" | "plain";

export function isBasemapMode(value: unknown): value is BasemapMode {
  return typeof value === "string" && (BASEMAP_MODES as readonly string[]).includes(value);
}

/** Modes that need the manifest before anything can be drawn. */
export function needsManifest(mode: BasemapMode): boolean {
  return mode === "auto" || mode === "protomaps";
}

/**
 * Kinds to try, in order; a base-map failure moves to the next one. `protomaps` never leaves the
 * origin (no OpenFreeMap fallback); `auto` uses the self-hosted tiles when they are installed.
 */
export function basemapChain(mode: BasemapMode, manifestAvailable: boolean): BasemapKind[] {
  switch (mode) {
    case "auto":
      return manifestAvailable ? ["protomaps", "openfreemap", "offline", "plain"] : ["openfreemap", "offline", "plain"];
    case "protomaps":
      return manifestAvailable ? ["protomaps", "offline", "plain"] : ["offline", "plain"];
    case "openfreemap":
      return ["openfreemap", "offline", "plain"];
    case "offline":
      return ["offline", "plain"];
  }
}

/** The tiles major version the bundled `@protomaps/basemaps` 5.x styles are written for. */
export const PROTOMAPS_TILES_MAJOR = 4;

/**
 * Relative path inside the base map directory: no scheme, no leading `/`, no `.`/`..` segment,
 * no backslash or `%` (so nothing can escape the directory or the origin once joined), no `{` / `}`
 * (MapLibre would substitute `{z}`, `{x}`, … in the `pmtiles://` tile URL built from the archive
 * path; the only template, `glyphs`, is a literal).
 */
const relativePath = z
  .string()
  .max(200)
  .regex(/^[A-Za-z0-9 ._@-]+(?:\/[A-Za-z0-9 ._@-]+)*$/, "must be a plain relative path")
  .refine((p) => !p.split("/").some((s) => s === "." || s === ".."), "must not contain dot segments");

const sha256 = z.string().regex(/^[0-9a-f]{64}$/);
const zoom = z.number().int().min(0).max(15);

/**
 * `manifest.json` (`keelson.basemap/1`), written last by `scripts/basemap/fetch-basemap.mjs`.
 * Strict: unknown keys (an `attribution` field, say) make the whole manifest invalid. It carries
 * no display text at all — attribution strings are code constants, because MapLibre renders them
 * as HTML and the manifest and the archive are third-party data.
 */
export const basemapManifestSchema = z.strictObject({
  schema: z.literal("keelson.basemap/1"),
  generated_at: z.iso.datetime(),
  profile: z.enum(["lite", "standard", "detail"]),
  source: z.strictObject({
    build: z.string().regex(/^\d{8}$/),
    basemap_version: z
      .string()
      .regex(/^\d+\.\d+\.\d+$/)
      .refine((v) => Number(v.split(".")[0]) === PROTOMAPS_TILES_MAJOR, `tiles major must be ${PROTOMAPS_TILES_MAJOR}`),
    url: z.string().regex(/^https:\/\/build\.protomaps\.com\/\d{8}\.pmtiles$/),
    b3sum: z
      .string()
      .regex(/^[0-9a-f]{64}$/)
      .nullable(),
  }),
  archive: z
    .strictObject({
      path: relativePath.refine((p) => p.endsWith(".pmtiles"), "must be a .pmtiles file"),
      bytes: z.number().int().positive(),
      sha256,
      minzoom: zoom,
      maxzoom: zoom,
    })
    .refine((a) => a.minzoom <= a.maxzoom, "minzoom must not exceed maxzoom"),
  glyphs: z.literal("fonts/{fontstack}/{range}.pbf"),
  fonts: z.array(z.string().regex(/^[A-Za-z0-9 ._-]+$/).max(80)).min(1).max(8),
  sprites: z.strictObject({ light: relativePath, dark: relativePath }),
  assets_commit: z.string().regex(/^[0-9a-f]{40}$/),
  licenses: z.array(relativePath).max(10),
});
export type BasemapManifest = z.infer<typeof basemapManifestSchema>;

/** The installed base map: its manifest and the directory every manifest path is relative to. */
export type InstalledBasemap = { manifest: BasemapManifest; baseUrl: string };

/** Directory URL of the manifest (`http://keelson.localhost/basemap/`), absolute. */
export function basemapBaseUrl(manifestUrl: string, pageUrl: string): string {
  return new URL(".", new URL(manifestUrl, pageUrl)).href;
}

/**
 * Joins a validated manifest path (or the glyphs template, whose `{fontstack}` / `{range}` must
 * stay literal for MapLibre) to the base directory. Plain concatenation on purpose: `new URL`
 * would percent-encode the braces.
 */
export function basemapAssetUrl(baseUrl: string, relative: string): string {
  return `${baseUrl}${relative}`;
}

const MAX_MANIFEST_BYTES = 64 * 1024;
const cache = new Map<string, Promise<InstalledBasemap | null>>();

export type LoadBasemapOptions = {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  /** Absolute page URL the manifest URL is resolved against (default `window.location.href`). */
  pageUrl?: string;
};

/**
 * Fetches and validates the manifest; `null` when it is absent (404, or the 204 "nothing
 * installed" answer of Caddy / keelson-web), unreachable, slow (3 s), redirected, too large or
 * invalid. Never throws. Cached per URL for the page's lifetime (one request per page).
 */
export function loadBasemapManifest(manifestUrl: string, options: LoadBasemapOptions = {}): Promise<InstalledBasemap | null> {
  const pageUrl = options.pageUrl ?? (typeof window === "undefined" ? "http://localhost/" : window.location.href);
  let absolute: string;
  try {
    absolute = new URL(manifestUrl, pageUrl).href;
  } catch {
    return Promise.resolve(null);
  }
  const cached = cache.get(absolute);
  if (cached) return cached;
  const promise = fetchManifest(absolute, options.fetchImpl ?? fetch, options.timeoutMs ?? 3000);
  cache.set(absolute, promise);
  return promise;
}

async function fetchManifest(url: string, fetchImpl: typeof fetch, timeoutMs: number): Promise<InstalledBasemap | null> {
  try {
    const res = await fetchImpl(url, {
      credentials: "same-origin",
      redirect: "error",
      cache: "no-cache",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (res.status !== 200) return null;
    const text = await res.text();
    if (text.length > MAX_MANIFEST_BYTES) return null;
    const parsed = basemapManifestSchema.safeParse(JSON.parse(text));
    return parsed.success ? { manifest: parsed.data, baseUrl: new URL(".", url).href } : null;
  } catch {
    return null;
  }
}

/** Forget cached manifests (tests; an app could call it after installing a base map). */
export function clearBasemapManifestCache(): void {
  cache.clear();
}

/**
 * Highest zoom the map allows per kind: the self-hosted archive is over-zoomed at most 5 levels
 * (z6 data stays readable to about z11), the 1:110m outline to z8; hosted styles keep MapLibre's.
 */
export function maxZoomFor(kind: BasemapKind, manifest: BasemapManifest | null): number | undefined {
  if (kind === "protomaps" && manifest) return manifest.archive.maxzoom + 5;
  if (kind === "offline") return 8;
  return undefined;
}
