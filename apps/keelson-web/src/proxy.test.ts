import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import { config, proxy } from "./proxy";

function req(path: string, cookie?: string) {
  return new NextRequest(new URL(path, "http://keelson.localhost"), {
    headers: cookie ? { cookie } : {},
  });
}

describe("proxy (session gate)", () => {
  it("sends anonymous visitors to the BFF sign-in with a same-site next", () => {
    const res = proxy(req("/cases/abc?tab=trace"));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("http://keelson.localhost/api/auth/login?next=%2Fcases%2Fabc%3Ftab%3Dtrace");
    expect(res.headers.get("cache-control")).toBe("no-store");
  });

  it("omits next for the home page", () => {
    expect(proxy(req("/")).headers.get("location")).toBe("http://keelson.localhost/api/auth/login");
  });

  it("lets signed-in visitors through and forwards the path", () => {
    const res = proxy(req("/cases", "keelson_session=abc"));
    expect(res.status).toBe(200);
    expect(res.headers.get("x-middleware-request-x-keelson-path")).toBe("/cases");
  });

  it("keeps the health probe public", () => {
    expect(proxy(req("/healthz")).status).toBe(200);
  });

  it("never runs for /api, build assets or static files", () => {
    const [matcher] = config.matcher;
    const re = new RegExp(`^${matcher}$`);
    expect(re.test("/cases/1")).toBe(true);
    expect(re.test("/")).toBe(true);
    expect(re.test("/api/me")).toBe(false);
    expect(re.test("/_next/static/chunk.js")).toBe(false);
    expect(re.test("/icon.svg")).toBe(false);
    expect(re.test("/robots.txt")).toBe(false);
    expect(re.test("/fonts/inter.woff2")).toBe(false);
    // the MapLibre worker (public/maplibre/*.mjs) is a static file, never redirected to sign-in
    expect(re.test("/maplibre/maplibre-gl-worker.mjs")).toBe(false);
    expect(re.test("/maplibre/maplibre-gl-shared.mjs")).toBe(false);
    expect(re.test("/cases/maplibre")).toBe(true);
    // the dot is literal: page paths that merely end in an asset-like word stay gated (regression)
    expect(re.test("/cases/0199png")).toBe(true);
    expect(re.test("/cases/robotsxtxt")).toBe(true);
    expect(re.test("/faviconxico")).toBe(true);
  });
});
