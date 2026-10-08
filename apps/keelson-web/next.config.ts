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

/**
 * Base-map tiles for the canvas network map (MapLibre style, TileJSON, vector / raster tiles,
 * sprites and glyphs are all fetched from here). OpenFreeMap by default (`@keelson/viz`
 * `DEFAULT_NETWORK_MAP_CONFIG`); nothing else leaves the origin.
 */
export const MAP_TILE_ORIGINS = ["https://tiles.openfreemap.org"];

/**
 * Everything else is same-origin: Next.js chunks, self-hosted Inter (@fontsource), `/api/*`
 * (fetch + EventSource) via Caddy, the MapLibre worker (`/maplibre/*.mjs`, module worker). The
 * App Router payload and the pre-paint theme script are inline scripts, Motion/Base UI and
 * MapLibre set inline styles, hence 'unsafe-inline'; `next dev` also needs eval.
 */
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  `img-src 'self' data: blob: ${MAP_TILE_ORIGINS.join(" ")}`,
  "font-src 'self' data:",
  `connect-src 'self' ${MAP_TILE_ORIGINS.join(" ")}${isDev ? " ws: wss:" : ""}`,
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
