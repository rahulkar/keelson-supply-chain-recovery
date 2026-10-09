/**
 * "No self-hosted base map here" (docs/contracts/127-m8-basemap.md). Caddy serves `/basemap/*`
 * from the `basemap` volume in compose; on Cloud Run a backend bucket does when deployed with
 * `--enable-basemap`. Without either, `/basemap/manifest.json` reaches this app: 204 (not a 404
 * page) lets the network map fall back silently, with no failed request in the browser console.
 */
export const dynamic = "force-dynamic";

export function GET(): Response {
  return new Response(null, { status: 204, headers: { "Cache-Control": "no-cache" } });
}
