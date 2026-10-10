import path from "node:path";
import type { NextConfig } from "next";

/**
 * Keelson web app. Served behind Caddy at http://keelson.localhost; `/api/*` on the same origin
 * is routed by Caddy straight to engine-api (SSE unbuffered), everything else to this app.
 *
 * The app lives in the pnpm workspace and consumes `@keelson/*` packages as TypeScript source,
 * so Turbopack and output tracing are rooted at the repository root (workspace symlinks resolve).
 */
const repoRoot = path.join(import.meta.dirname, "..", "..");
const isDev = process.env.NODE_ENV !== "production";

/** OpenFreeMap: the hosted base map of the `auto` / `openfreemap` modes (`@keelson/viz` `styleUrls`). */
export const DEFAULT_MAP_TILE_ORIGINS: readonly string[] = ["https://tiles.openfreemap.org"];

/**
 * Third-party base-map origins the CSP allows for the canvas network map (MapLibre style, TileJSON,
 * tiles, sprites and glyphs are fetched from there), from the BUILD-time env
 * `KEELSON_CSP_MAP_ORIGINS` (`headers()` is compiled into the standalone server): space-separated
 * https origins, unset/empty → OpenFreeMap, `none` → no third-party origin (strict / air-gapped
 * build: the self-hosted Protomaps map and the built-in outline are same-origin; the `openfreemap`
 * mode then falls back to the outline). docs/contracts/127-m8-basemap.md. Throws (fails the build)
 * on anything that is not a plain https origin.
 */
export function mapTileOrigins(raw: string | undefined): string[] {
  const value = raw?.trim() ?? "";
  if (value === "") return [...DEFAULT_MAP_TILE_ORIGINS];
  if (value === "none") return [];
  const origins = value.split(/\s+/);
  for (const origin of origins) {
    if (!/^https:\/\/[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?$/.test(origin)) {
      throw new Error(`KEELSON_CSP_MAP_ORIGINS: "${origin}" is not an https origin (e.g. https://tiles.example.com), or use "none"`);
    }
  }
  return origins;
}

export const MAP_TILE_ORIGINS = mapTileOrigins(process.env.KEELSON_CSP_MAP_ORIGINS);
const mapSources = MAP_TILE_ORIGINS.map((o) => ` ${o}`).join("");

/**
 * Everything else is same-origin: Next.js chunks, self-hosted Inter (@fontsource), `/api/*`
 * (fetch + EventSource) via Caddy, the MapLibre worker (`/maplibre/*.mjs`, module worker), the
 * self-hosted base map (`/basemap/*`: manifest, pmtiles byte ranges, glyphs, sprite). The
 * App Router payload and the pre-paint theme script are inline scripts, Motion/Base UI and
 * MapLibre set inline styles, hence 'unsafe-inline'; `next dev` also needs eval.
 */
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  `img-src 'self' data: blob:${mapSources}`,
  "font-src 'self' data:",
  `connect-src 'self'${mapSources}${isDev ? " ws: wss:" : ""}`,
  "worker-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  reactCompiler: true,
  poweredByHeader: false,
  reactStrictMode: true,
  transpilePackages: ["@keelson/ui", "@keelson/viz", "@keelson/tokens", "@keelson/contracts"],
  outputFileTracingRoot: repoRoot,
  turbopack: { root: repoRoot },
  typedRoutes: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
