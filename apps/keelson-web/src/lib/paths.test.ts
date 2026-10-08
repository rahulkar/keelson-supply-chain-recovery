import { describe, expect, it } from "vitest";
import { connectUrl, loginUrl, safeConnectUrl, safeReturnPath } from "./paths";

describe("safeReturnPath", () => {
  it.each([
    ["/cases/abc?x=1#y", "/cases/abc?x=1#y"],
    ["/", "/"],
    ["//evil.example/x", "/"],
    ["https://evil.example/", "/"],
    ["/\\evil.example", "/"],
    ["javascript:alert(1)", "/"],
    ["/ok\nSet-Cookie: x", "/"],
    ["", "/"],
    [null, "/"],
    ["/" + "a".repeat(2100), "/"],
  ])("%j → %j", (input, expected) => {
    expect(safeReturnPath(input)).toBe(expected);
  });
});

describe("loginUrl", () => {
  it("carries a safe next path", () => {
    expect(loginUrl("/cases/1")).toBe("/api/auth/login?next=%2Fcases%2F1");
    expect(loginUrl("/")).toBe("/api/auth/login");
    expect(loginUrl("//evil")).toBe("/api/auth/login");
  });
});

describe("connectUrl / safeConnectUrl", () => {
  const origin = "http://keelson.localhost";
  const fallback = { connector: "merxa", returnTo: "/cases/c1" };

  it("builds the Connect-Merxa URL", () => {
    expect(connectUrl("merxa", "/cases/c1")).toBe("/api/connectors/merxa/connect?return_to=%2Fcases%2Fc1");
    expect(connectUrl("../x", "//evil")).toBe("/api/connectors/merxa/connect?return_to=%2F");
  });

  it("accepts the engine's absolute same-origin connect_url", () => {
    expect(
      safeConnectUrl("http://keelson.localhost/api/connectors/merxa/connect?return_to=%2Fcases%2Fc1", origin, fallback),
    ).toBe("/api/connectors/merxa/connect?return_to=%2Fcases%2Fc1");
    expect(safeConnectUrl("/api/connectors/merxa/connect?return_to=/cases/c2", origin, fallback)).toBe(
      "/api/connectors/merxa/connect?return_to=%2Fcases%2Fc2",
    );
  });

  it("never follows a foreign or non-connect URL", () => {
    const ours = "/api/connectors/merxa/connect?return_to=%2Fcases%2Fc1";
    expect(safeConnectUrl("http://evil.example/api/connectors/merxa/connect", origin, fallback)).toBe(ours);
    expect(safeConnectUrl("http://keelson.localhost/api/auth/logout", origin, fallback)).toBe(ours);
    expect(safeConnectUrl("http://keelson.localhost/api/connectors/merxa/connect?return_to=https://evil.example", origin, fallback)).toBe(ours);
    expect(safeConnectUrl(42, origin, fallback)).toBe(ours);
  });
});
