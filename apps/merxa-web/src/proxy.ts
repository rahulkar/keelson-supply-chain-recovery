import { NextResponse, type NextRequest } from "next/server";

/**
 * Session gate: pages need the `merxa_session` cookie (validated by merxa-api on every
 * data call). Anonymous visitors go to /login with a same-site `next` target.
 * Also forwards the requested path to server components (`x-merxa-path`).
 */
const SESSION_COOKIE = "merxa_session";
const PUBLIC_PATHS = ["/login", "/healthz"];

function isPublic(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const target = `${pathname}${search}`;
  if (!isPublic(pathname) && !request.cookies.get(SESSION_COOKIE)?.value) {
    // nextUrl carries the public origin (Host forwarded by Caddy), so the redirect stays on it.
    const login = request.nextUrl.clone();
    login.pathname = "/login";
    login.search = pathname === "/" ? "" : `?next=${encodeURIComponent(target)}`;
    const res = NextResponse.redirect(login, 307);
    res.headers.set("Cache-Control", "no-store");
    return res;
  }
  const forwarded = new Headers(request.headers);
  forwarded.set("x-merxa-path", target);
  return NextResponse.next({ request: { headers: forwarded } });
}

export const config = {
  // Pages only: not static assets, not `/v1` (served by merxa-api via Caddy).
  matcher: ["/((?!_next/static|_next/image|v1/|favicon.ico|icon.svg|.*\\.(?:svg|png|ico|woff2?)$).*)"],
};
