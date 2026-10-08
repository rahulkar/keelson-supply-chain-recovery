/**
 * Browser-side merxa-api client: same-origin `/v1/*` (routed by Caddy to merxa-api) with
 * the HttpOnly `merxa_session` cookie. Writes rely on merxa-api's Origin check (CSRF) and
 * send `If-Match` / `Idempotency-Key` as the REST contract requires.
 */
import { ApiError, buildQuery, readProblem, type Query } from "./http";

type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: Query;
  body?: unknown;
  ifMatch?: number;
  idempotencyKey?: string;
  signal?: AbortSignal;
};

export async function apiFetch<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { accept: "application/json" };
  if (opts.body !== undefined) headers["content-type"] = "application/json";
  if (opts.ifMatch !== undefined) headers["if-match"] = `"${opts.ifMatch}"`;
  if (opts.idempotencyKey) headers["idempotency-key"] = opts.idempotencyKey;
  let res: Response;
  try {
    res = await fetch(`${path}${buildQuery(opts.query)}`, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: "same-origin",
      cache: "no-store",
      signal: opts.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, {
      status: 0,
      code: "network_error",
      title: "Network error",
      detail: err instanceof Error ? err.message : String(err),
    });
  }
  if (!res.ok) throw new ApiError(res.status, await readProblem(res));
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
