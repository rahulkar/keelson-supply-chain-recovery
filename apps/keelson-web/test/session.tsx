import type { ReactNode } from "react";
import { vi } from "vitest";
import { ApiClient } from "@/lib/api/client";
import type { CaseDetail, Me } from "@/lib/api/types";
import { SessionProvider } from "@/lib/client/session";

export function makeMe(over: Partial<Me["user"]> = {}, demo = true): Me {
  return {
    user: {
      sub: "sub-priya",
      username: "priya",
      name: "Priya Shah",
      email: "priya@example.test",
      roles: ["planner"],
      tenants: ["demo-retail"],
      active_tenant: "demo-retail",
      ...over,
    },
    csrf_token: "csrf-test",
    demo_mode: demo,
    brand: { engine: "Keelson", vendor: "Merxa" },
  };
}

export type Route = (url: string, init: RequestInit) => Response | Promise<Response> | undefined;

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** An ApiClient whose fetch is answered by `routes` (first match wins); records every call. */
export function fakeApi(routes: Route[]) {
  const calls: { url: string; method: string; headers: Record<string, string>; body: unknown }[] = [];
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    calls.push({
      url,
      method: init.method ?? "GET",
      headers: (init.headers ?? {}) as Record<string, string>,
      body: init.body instanceof FormData ? init.body : init.body ? JSON.parse(String(init.body)) : undefined,
    });
    for (const route of routes) {
      const res = await route(url, init);
      if (res) return res;
    }
    return jsonResponse(404, { code: "not_found", message: `no fake route for ${url}` });
  });
  const api = new ApiClient({ fetch: fetchImpl as unknown as typeof fetch, csrfToken: "csrf-test", onUnauthorized: () => {} });
  return { api, calls };
}

export function withSession(me: Me, api: ApiClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <SessionProvider me={me} client={api}>
        {children}
      </SessionProvider>
    );
  };
}

export function caseDetail(caseId: string, over: Partial<CaseDetail> = {}): CaseDetail {
  return {
    case: {
      id: caseId,
      title: "Trim delay",
      mode: "actual",
      status: "open",
      created_by: { sub: "sub-priya" },
      created_at: "2026-10-05T09:00:00Z",
    },
    messages: [],
    blocks: [],
    open_interrupts: [],
    last_event_seq: 0,
    ...over,
  };
}
