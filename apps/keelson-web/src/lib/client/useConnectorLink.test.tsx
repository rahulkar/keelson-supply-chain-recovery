import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { connectReturnOutcome, useConnectorLink } from "./useConnectorLink";

type Link = Awaited<ReturnType<ApiClient["connectorLink"]>>;

function fakeApi(answers: (Link | Error)[]) {
  const connectorLink = vi.fn(async () => {
    const next = answers.length > 1 ? answers.shift()! : answers[0]!;
    if (next instanceof Error) throw next;
    return next;
  });
  return { api: { connectorLink } as unknown as ApiClient, connectorLink };
}

const READY: Link = { status: "active", applyReady: true, extUsername: "marco" };
const MISSING: Link = { status: "missing", applyReady: false, extUsername: null };

describe("useConnectorLink", () => {
  it("does nothing until enabled, then checks once: checking → the viewer's link", async () => {
    const { api, connectorLink } = fakeApi([MISSING]);
    const { result, rerender } = renderHook(({ on }) => useConnectorLink(api, "merxa", on), { initialProps: { on: false } });
    expect(result.current).toEqual({ status: "unknown" });
    expect(connectorLink).not.toHaveBeenCalled();
    rerender({ on: true });
    expect(result.current).toEqual({ status: "checking" });
    await waitFor(() => expect(result.current).toEqual({ status: "missing", applyReady: false }));
    expect(connectorLink).toHaveBeenCalledTimes(1);
    expect(connectorLink).toHaveBeenCalledWith("merxa");
  });

  it("a failed check reads unknown (the server's 412 decides)", async () => {
    const { api } = fakeApi([new ApiError(500, "internal_error", "x")]);
    const { result } = renderHook(() => useConnectorLink(api, "merxa", true));
    await waitFor(() => expect(result.current).toEqual({ status: "unknown" }));
  });

  it("re-checks when the tab becomes visible or the page is restored, keeping the last answer meanwhile", async () => {
    const { api, connectorLink } = fakeApi([MISSING, READY]);
    const { result } = renderHook(() => useConnectorLink(api, "merxa", true));
    await waitFor(() => expect(result.current).toEqual({ status: "missing", applyReady: false }));
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current).toEqual({ status: "missing", applyReady: false });
    await waitFor(() => expect(result.current).toEqual({ status: "active", applyReady: true }));
    const restored = new Event("pageshow") as PageTransitionEvent;
    Object.defineProperty(restored, "persisted", { value: true });
    act(() => {
      window.dispatchEvent(restored);
    });
    await waitFor(() => expect(connectorLink).toHaveBeenCalledTimes(3));
  });
});

describe("connectReturnOutcome", () => {
  it("reads ?connected / ?connect_error and strips them, keeping other parameters", () => {
    expect(connectReturnOutcome("http://keelson.localhost/cases/c1?connected=merxa&tab=x#t")).toEqual({
      outcome: { ok: true, connector: "Merxa" },
      cleanUrl: "/cases/c1?tab=x#t",
    });
    expect(connectReturnOutcome("http://keelson.localhost/cases/c1?connect_error=denied")).toEqual({
      outcome: { ok: false, message: "The Merxa sign-in was cancelled." },
      cleanUrl: "/cases/c1",
    });
    // free text from the query string is never echoed
    expect(connectReturnOutcome("http://keelson.localhost/?connect_error=%3Cscript%3E")?.outcome).toEqual({
      ok: false,
      message: "Merxa could not be connected. Try again.",
    });
    expect(connectReturnOutcome("http://keelson.localhost/?connected=%3Cb%3Eevil")?.outcome).toEqual({ ok: true, connector: "The connector" });
    expect(connectReturnOutcome("http://keelson.localhost/cases/c1")).toBeNull();
    expect(connectReturnOutcome("not a url")).toBeNull();
  });
});
