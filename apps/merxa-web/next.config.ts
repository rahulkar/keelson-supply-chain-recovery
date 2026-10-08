import type { NextConfig } from "next";

/**
 * Merxa Retail Suite web UI. Served behind Caddy at http://merxa.localhost; `/v1/*` on the
 * same origin is routed by Caddy to merxa-api, everything else to this app (port 3001).
 */
const isDev = process.env.NODE_ENV !== "production";

/**
 * Everything is same-origin: Next.js chunks, self-hosted fonts (@fontsource), `/v1/*` via
 * Caddy. Inline scripts carry the App Router payload and AG Grid injects its theme CSS at
 * runtime, hence 'unsafe-inline'; `next dev` additionally needs eval.
 */
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self'${isDev ? " ws: wss:" : ""}`,
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
  poweredByHeader: false,
  reactStrictMode: true,
  // The app lives inside the Keelson monorepo but is a standalone project: trace from here.
  outputFileTracingRoot: import.meta.dirname,
  turbopack: { root: import.meta.dirname },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
