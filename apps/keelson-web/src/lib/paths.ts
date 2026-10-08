/**
 * Same-site navigation helpers. Every "come back here" value is reduced to an absolute path on
 * this origin (never `//host`, a scheme or a backslash trick), mirroring the engine's
 * `safe_return_path`, so no URL taken from a block, an API error or a query string can turn
 * into an open redirect.
 */

const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

/** `value` when it is a same-site absolute path (`/x?y#z`), else `fallback`. */
export function safeReturnPath(value: string | null | undefined, fallback = "/"): string {
  if (!value || value.length > 2000) return fallback;
  if (!value.startsWith("/") || value.startsWith("//") || value.includes("\\") || CONTROL_CHARS.test(value)) {
    return fallback;
  }
  try {
    const url = new URL(value, "http://keelson.invalid");
    if (url.origin !== "http://keelson.invalid") return fallback;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}

/** BFF sign-in (OIDC code + PKCE), returning to `next` afterwards. */
export function loginUrl(next: string | null | undefined): string {
  const target = safeReturnPath(next);
  return target === "/" ? "/api/auth/login" : `/api/auth/login?next=${encodeURIComponent(target)}`;
}

/** Start the delegated Connect-Merxa flow, coming back to `returnTo`. */
export function connectUrl(connector: string, returnTo: string | null | undefined): string {
  const slug = /^[a-z0-9][a-z0-9-]{0,62}$/.test(connector) ? connector : "merxa";
  return `/api/connectors/${slug}/connect?return_to=${encodeURIComponent(safeReturnPath(returnTo))}`;
}

/**
 * A connect URL from an API error (`412 {code:"connect_required", connect_url}`), accepted only
 * when it is this origin's `/api/connectors/<slug>/connect` route (absolute or relative).
 * Anything else falls back to the URL we build ourselves.
 */
export function safeConnectUrl(
  candidate: unknown,
  origin: string,
  fallback: { connector: string; returnTo: string },
): string {
  if (typeof candidate === "string" && candidate) {
    try {
      const url = new URL(candidate, origin);
      if (url.origin === origin && /^\/api\/connectors\/[a-z0-9-]+\/connect$/.test(url.pathname) && !url.username) {
        const back = safeReturnPath(url.searchParams.get("return_to"), fallback.returnTo);
        return `${url.pathname}?return_to=${encodeURIComponent(back)}`;
      }
    } catch {
      // fall through
    }
  }
  return connectUrl(fallback.connector, fallback.returnTo);
}
