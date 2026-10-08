// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { proxy } from "./proxy";

function request(path: string, cookie?: string): NextRequest {
  return new NextRequest(`http://merxa.localhost${path}`, { headers: cookie ? { cookie } : {} });
}

describe("proxy (session gate)", () => {
  it("redirects anonymous page requests to /login with a next target on the public origin", () => {
    const res = proxy(request("/inventory?dc=UK1"));
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toBe("http://merxa.localhost/login?next=%2Finventory%3Fdc%3DUK1");
  });

  it("sends the root straight to /login", () => {
    expect(proxy(request("/")).headers.get("location")).toBe("http://merxa.localhost/login");
  });

  it("lets public paths through", () => {
    expect(proxy(request("/login")).headers.get("location")).toBeNull();
    expect(proxy(request("/healthz")).headers.get("location")).toBeNull();
  });

  it("forwards the requested path to server components when a session cookie is present", () => {
    const res = proxy(request("/purchase-orders/PO-BD-0001?line=2", "merxa_session=abc"));
    expect(res.headers.get("location")).toBeNull();
    expect(res.headers.get("x-middleware-request-x-merxa-path")).toBe("/purchase-orders/PO-BD-0001?line=2");
  });
});
