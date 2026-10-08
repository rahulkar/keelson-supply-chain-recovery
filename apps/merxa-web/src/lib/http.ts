/** Shared HTTP helpers for server and browser API clients (no Node/browser-only APIs). */
import type { ProblemDocument } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly problem: ProblemDocument;

  constructor(status: number, problem: ProblemDocument) {
    super(problem.detail || problem.title || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = problem.code || (status === 0 ? "network_error" : `http_${status}`);
    this.problem = problem;
  }
}

export type QueryValue = string | number | boolean | null | undefined | readonly (string | number)[];
export type Query = Record<string, QueryValue>;

/** `{a: 1, b: ['x','y'], c: undefined}` → "?a=1&b=x&b=y" (empty → ""). Repeated keys for arrays. */
export function buildQuery(query: Query | undefined): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const v of value) if (v !== "" && v !== null && v !== undefined) params.append(key, String(v));
    } else {
      params.append(key, String(value));
    }
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

/** Parses an error response into a problem document, tolerating non-JSON bodies. */
export async function readProblem(res: Response): Promise<ProblemDocument> {
  const text = await res.text().catch(() => "");
  if (text) {
    try {
      const doc: unknown = JSON.parse(text);
      if (doc && typeof doc === "object" && !Array.isArray(doc)) {
        const p = doc as ProblemDocument & { detail?: unknown };
        // FastAPI validation errors use `detail: [...]`.
        if (Array.isArray(p.detail)) {
          return { status: res.status, code: "invalid_request", title: "Invalid request", detail: JSON.stringify(p.detail) };
        }
        return { status: res.status, ...p };
      }
    } catch {
      // fall through to plain text
    }
  }
  return { status: res.status, title: res.statusText || `HTTP ${res.status}`, detail: text.slice(0, 300) || undefined };
}

/** Human message for an error (problem detail first). */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.problem.detail || err.problem.title || err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * Only same-site absolute paths are accepted as post-login targets (mirrors merxa-api
 * `_safe_next`): no scheme, no protocol-relative `//`, no backslashes, no control chars.
 */
export function safeNext(value: string | null | undefined, fallback = "/change-batches"): string {
  if (!value || value.length > 2048 || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return fallback;
  }
  for (const ch of value) {
    const c = ch.codePointAt(0) ?? 0;
    if (c <= 0x20 || c === 0x7f) return fallback;
  }
  if (value === "/login" || value.startsWith("/login?") || value.startsWith("/login/")) return fallback;
  return value;
}

export function loginPath(next: string | null | undefined): string {
  const target = safeNext(next, "");
  return target ? `/login?next=${encodeURIComponent(target)}` : "/login";
}

/**
 * Operator-guide `deep_link`s are absolute URLs built by merxa-api from its public URL plus
 * values taken from change-set payloads (submitted by integrations). They are rendered as
 * in-app links: only http(s) URLs are accepted and only their path/query/fragment is kept,
 * so a link can never leave the Merxa origin (and works behind any host name).
 */
export function appPath(link: string | null | undefined): string | null {
  if (!link) return null;
  let url: URL;
  try {
    url = new URL(link, "http://merxa.invalid");
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const path = `${url.pathname}${url.search}${url.hash}`;
  return safeNext(path, "") || null;
}

/** Identity-provider logout URL from merxa-api: only http(s) targets are followed. */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

/** RFC 6265 cookie-octets only (the session token is URL-safe base64), bounded length. */
export function isForwardableCookieValue(value: string | null | undefined): value is string {
  return !!value && value.length <= 4096 && /^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+$/.test(value);
}

/** Route params may arrive percent-encoded depending on the router path; never throws. */
export function decodeParam(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
