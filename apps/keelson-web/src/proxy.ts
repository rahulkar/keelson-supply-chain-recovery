import { NextResponse, type NextRequest } from "next/server";
import { loginUrl } from "./lib/paths";

/**
 * Session gate (Next 16 `proxy`): pages need the `keelson_session` cookie (validated by engine-api
 * on every data call; an expired one is caught server-side by the app layout). Anonymous visitors
 * go to the BFF sign-in with a same-site `next`. The requested path is forwarded to Server
 * Components (`x-keelson-path`) so they can send the user back after sign-in.
 */
export const SESSION_COOKIE = "keelson_session";
const PUBLIC_PATHS = ["/healthz"];

function isPublic(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export function proxy(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  const target = `${pathname}${search}`;
  if (!isPublic(pathname) && !request.cookies.get(SESSION_COOKIE)?.value) {
    // nextUrl carries the public origin (Caddy forwards the Host header), so the redirect stays on it.
    const login = new URL(loginUrl(target), request.nextUrl.origin);
    const res = NextResponse.redirect(login, 307);
    res.headers.set("Cache-Control", "no-store");
    return res;
  }
  const forwarded = new Headers(request.headers);
  forwarded.set("x-keelson-path", target);
  return NextResponse.next({ request: { headers: forwarded } });
}

export const config = {
  // Pages only: not build assets, not `/api` (served by engine-api via Caddy), not static files.
  // `\\.` in this (statically analysed, so plain literal) string is a literal dot in the regular
  // expression; a bare `\.` would be just `.` and let e.g. `/cases/xpng` skip the session gate.
  // `maplibre/` is the MapLibre worker copied into public/ (static, loaded by a module Worker).
  // `basemap/` is the self-hosted base map (Caddy or a bucket serve it; when neither does, the
  // public "nothing installed" manifest route answers here, never a sign-in redirect).
  matcher: ["/((?!_next/static|_next/image|api/|maplibre/|basemap/|favicon\\.ico$|icon\\.svg$|robots\\.txt$|.*\\.(?:svg|png|ico|woff2?|txt)$).*)"],
};
