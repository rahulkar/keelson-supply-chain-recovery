/** Liveness probe for the web process (does not call merxa-api). */
export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ status: "ok", service: "merxa-web" }, { headers: { "Cache-Control": "no-store" } });
}
