import "server-only";

import { cookies, headers } from "next/headers";
import { notFound, redirect } from "next/navigation";
import { cache } from "react";

import { ApiError, buildQuery, isForwardableCookieValue, loginPath, readProblem, type Query } from "./http";
import type { Me, Page } from "./types";

/**
 * Server-side merxa-api client. Server components call merxa-api directly on the
 * internal network (MERXA_API_INTERNAL_URL) forwarding only the `merxa_session` cookie;
 * the browser uses same-origin `/v1/*` through Caddy.
 */

export const SESSION_COOKIE = "merxa_session";
/** Set by `proxy.ts` so server components know the page to return to after login. */
export const PATH_HEADER = "x-merxa-path";

const DEFAULT_TIMEOUT_MS = 15_000;

export function apiBaseUrl(): string {
  return (process.env.MERXA_API_INTERNAL_URL || "http://merxa-api:8100").replace(/\/+$/, "");
}

async function currentPath(): Promise<string> {
  const h = await headers();
  return h.get(PATH_HEADER) || "/";
}

export async function redirectToLogin(): Promise<never> {
  redirect(loginPath(await currentPath()));
}

type GetOptions = {
  /** `notFound()` on 404 instead of throwing (detail pages). */
  notFoundOn404?: boolean;
  /** Return null instead of redirecting on 401 (login page). */
  allowAnonymous?: boolean;
};

async function rawGet(path: string, query?: Query): Promise<Response> {
  const jar = await cookies();
  const session = jar.get(SESSION_COOKIE)?.value;
  const reqHeaders: Record<string, string> = { accept: "application/json" };
  // Only the session cookie is forwarded, and only when it is a well-formed cookie value.
  if (isForwardableCookieValue(session)) reqHeaders.cookie = `${SESSION_COOKIE}=${session}`;
  try {
    return await fetch(`${apiBaseUrl()}${path}${buildQuery(query)}`, {
      headers: reqHeaders,
      cache: "no-store",
      signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    });
  } catch (err) {
    throw new ApiError(0, {
      status: 0,
      code: "merxa_api_unreachable",
      title: "Merxa services are unreachable",
      detail: err instanceof Error ? err.message : String(err),
    });
  }
}

export async function apiGet<T>(path: string, query?: Query, opts: GetOptions = {}): Promise<T> {
  const res = await rawGet(path, query);
  if (res.ok) return (await res.json()) as T;
  if (res.status === 401) {
    if (opts.allowAnonymous) throw new ApiError(401, await readProblem(res));
    return redirectToLogin();
  }
  if (res.status === 404 && opts.notFoundOn404) notFound();
  throw new ApiError(res.status, await readProblem(res));
}

/** Follows `next_cursor` until exhausted or `max` rows (API page size is capped at 1000). */
export async function apiGetAll<T>(
  path: string,
  query: Query = {},
  { max = 5000, pageSize = 1000 }: { max?: number; pageSize?: number } = {},
): Promise<{ items: T[]; truncated: boolean }> {
  const items: T[] = [];
  let cursor: string | null = null;
  do {
    const page: Page<T> = await apiGet<Page<T>>(path, {
      ...query,
      limit: Math.min(pageSize, max - items.length),
      cursor: cursor ?? undefined,
    });
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor && items.length < max);
  return { items, truncated: cursor !== null };
}

/**
 * Walks every page of a keyset-paginated listing (`next_cursor`), handing each page to
 * `onPage`, until the listing is exhausted or `maxRows` rows were read. `complete` is false
 * when the cap stopped the walk.
 */
export async function apiWalk<T>(
  path: string,
  query: Query,
  onPage: (items: T[]) => void,
  { maxRows = 100_000, pageSize = 1000 }: { maxRows?: number; pageSize?: number } = {},
): Promise<{ rows: number; complete: boolean }> {
  let rows = 0;
  let cursor: string | null = null;
  do {
    const page: Page<T> = await apiGet<Page<T>>(path, { ...query, limit: pageSize, cursor: cursor ?? undefined });
    rows += page.items.length;
    onPage(page.items);
    cursor = page.next_cursor;
  } while (cursor && rows < maxRows);
  return { rows, complete: cursor === null };
}

/**
 * The newest `keep` rows of an ascending listing (e.g. `/v1/audit-log`, which only pages
 * oldest → newest). Rows stay in API order. `omittedOlder`: older rows were dropped;
 * `complete: false`: the walk hit `maxRows`, so even newer rows may be missing.
 */
export async function apiGetLatest<T>(
  path: string,
  query: Query = {},
  { keep = 5000, maxRows = 100_000 }: { keep?: number; maxRows?: number } = {},
): Promise<{ items: T[]; omittedOlder: boolean; complete: boolean }> {
  let items: T[] = [];
  const { rows, complete } = await apiWalk<T>(
    path,
    query,
    (page) => {
      items.push(...page);
      if (items.length > 2 * keep) items = items.slice(-keep);
    },
    { maxRows },
  );
  if (items.length > keep) items = items.slice(-keep);
  return { items, omittedOlder: rows > keep, complete };
}

export type Loaded<T> = { ok: true; data: T } | { ok: false; error: ApiError };

/** Wraps a load so pages can render an inline problem panel instead of the error boundary. */
export async function load<T>(promise: Promise<T>): Promise<Loaded<T>> {
  try {
    return { ok: true, data: await promise };
  } catch (err) {
    if (err instanceof ApiError) return { ok: false, error: err };
    throw err; // redirect()/notFound() signals and programming errors propagate
  }
}

/** Current session (deduplicated per request). Redirects to /login when there is none. */
export const getMe = cache(async (): Promise<Me> => apiGet<Me>("/v1/auth/me"));

/** Current session or null (no redirect); used by the login page. */
export const findMe = cache(async (): Promise<Me | null> => {
  try {
    return await apiGet<Me>("/v1/auth/me", undefined, { allowAnonymous: true });
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return null;
    throw err;
  }
});

/**
 * The simulation's current ISO week (Merxa runs on a demo clock, not the wall clock).
 * merxa-api does not expose the clock directly; `GET /v1/lane-options` echoes it as `week`
 * when no week is given (any origin/dest is accepted, unknown ones just have no options).
 * Null when it cannot be read (e.g. no po:read).
 */
export const getSimWeek = cache(async (): Promise<string | null> => {
  try {
    const res = await apiGet<{ week?: unknown }>("/v1/lane-options", { origin: "-", dest: "-" });
    return typeof res.week === "string" && /^\d{4}-W\d{2}$/.test(res.week) ? res.week : null;
  } catch (err) {
    if (err instanceof ApiError) return null;
    throw err;
  }
});
