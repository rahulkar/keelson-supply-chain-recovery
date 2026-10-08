/** Hosts that block links (narrative, deep links) may point at (`65-web-internals.md`). */
export const ALLOWED_LINK_HOSTS = ["merxa.localhost", "keelson.localhost"] as const;

/** Prefixes handed to rehype-harden (http only in dev; https accepted for deployed hosts). */
export const ALLOWED_LINK_PREFIXES: string[] = ALLOWED_LINK_HOSTS.flatMap((h) => [`http://${h}`, `https://${h}`]);

/**
 * Machine endpoints on the allowlisted hosts (`00-overview.md` routing). Links into them are never
 * legitimate in a block — people navigate to pages — and some have GET side effects (e.g.
 * `/api/connectors/merxa/connect?return_to=…` starts an OAuth hop), so narrative text that an
 * injected document influenced must not be able to point at them.
 */
const BLOCKED_PATH_PREFIXES: Record<(typeof ALLOWED_LINK_HOSTS)[number], readonly string[]> = {
  "keelson.localhost": ["/api/"],
  "merxa.localhost": ["/v1/", "/mcp", "/.well-known/"],
};

/**
 * `true` for absolute http(s) URLs on an allowlisted host (exact hostname match, any port, no
 * credentials) that point at a page rather than an API/MCP endpoint.
 */
export function isAllowedHref(href: string | null | undefined): href is string {
  if (!href) return false;
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return false;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  if (url.username || url.password) return false;
  const host = url.hostname as (typeof ALLOWED_LINK_HOSTS)[number];
  if (!(ALLOWED_LINK_HOSTS as readonly string[]).includes(host)) return false;
  // `URL` has already resolved dot segments and percent-decoding of the path is not needed:
  // an encoded "/%61pi/" is not routed to the API by Caddy either.
  const path = url.pathname.toLowerCase();
  return !BLOCKED_PATH_PREFIXES[host].some((prefix) => path === prefix.replace(/\/$/, "") || path.startsWith(prefix));
}

/** Where to come back after an OAuth hop (current path, same-origin only). */
export function currentReturnTo(): string {
  if (typeof window === "undefined") return "/";
  const { pathname, search, hash } = window.location;
  return `${pathname}${search}${hash}` || "/";
}
