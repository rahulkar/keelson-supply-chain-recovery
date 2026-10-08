import "server-only";

/**
 * Server-side reads from engine-api for Server Components (first paint, auth and role gates).
 *
 * The browser talks to engine-api same-origin through Caddy; on the server we call it directly
 * on the internal network (`ENGINE_API_INTERNAL_URL`, compose: http://engine-api:8000) and
 * forward only the `keelson_session` cookie. Server code never mutates (no CSRF token needed).
 */
import { cookies } from "next/headers";
import { cache } from "react";
import type { z } from "zod";
import { caseDetailSchema, caseListSchema, meSchema, type CaseDetail, type CaseSummary, type Me } from "../api/types";
import { isResourceId } from "../ids";

export const SESSION_COOKIE = "keelson_session";

export type EngineResult<T> =
  | { kind: "ok"; data: T }
  | { kind: "unauthenticated" }
  | { kind: "forbidden" }
  | { kind: "not_found" }
  | { kind: "unavailable"; detail: string };

function engineBase(): string {
  const raw = process.env.ENGINE_API_INTERNAL_URL || "http://engine-api:8000";
  return raw.replace(/\/+$/, "");
}

async function engineGet<S extends z.ZodType>(path: string, schema: S): Promise<EngineResult<z.infer<S>>> {
  const session = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!session) return { kind: "unauthenticated" };
  let res: Response;
  try {
    res = await fetch(`${engineBase()}${path}`, {
      headers: { Accept: "application/json", Cookie: `${SESSION_COOKIE}=${session}` },
      cache: "no-store",
      // The session cookie is only for engine-api's answer to this exact path: never follow a
      // redirect with it (engine-api has no redirects on these reads).
      redirect: "manual",
      signal: AbortSignal.timeout(8000),
    });
  } catch (err) {
    return { kind: "unavailable", detail: err instanceof Error ? err.message : "engine-api unreachable" };
  }
  if (res.status === 401) return { kind: "unauthenticated" };
  if (res.status === 403) return { kind: "forbidden" };
  if (res.status === 404) return { kind: "not_found" };
  if (res.status >= 300 && res.status < 400) return { kind: "unavailable", detail: `engine-api redirected (${res.status})` };
  if (!res.ok) return { kind: "unavailable", detail: `engine-api answered ${res.status}` };
  let json: unknown;
  try {
    json = await res.json();
  } catch {
    return { kind: "unavailable", detail: "engine-api sent invalid JSON" };
  }
  const parsed = schema.safeParse(json);
  if (!parsed.success) return { kind: "unavailable", detail: "engine-api sent an unexpected response" };
  return { kind: "ok", data: parsed.data };
}

/** `GET /api/me` once per request. */
export const getMe = cache((): Promise<EngineResult<Me>> => engineGet("/api/me", meSchema));

export const getCase = cache(
  async (caseId: string): Promise<EngineResult<CaseDetail>> =>
    isResourceId(caseId) ? engineGet(`/api/cases/${encodeURIComponent(caseId)}`, caseDetailSchema) : { kind: "not_found" },
);

export async function listCases(limit = 50): Promise<EngineResult<CaseSummary[]>> {
  const n = Math.min(200, Math.max(1, Math.floor(limit)));
  const out = await engineGet(`/api/cases?limit=${n}`, caseListSchema);
  return out.kind === "ok" ? { kind: "ok", data: out.data.items } : out;
}
