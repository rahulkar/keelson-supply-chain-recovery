/** Liveness probe for the web process (does not call engine-api). */
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json({ status: "ok", service: "keelson-web" }, { headers: { "Cache-Control": "no-store" } });
}
