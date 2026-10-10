/**
 * `pmtiles://` for MapLibre: one protocol instance per page, registered once. The handler runs on
 * the main thread and reads the archive with HTTP Range requests from our own origin (CSP
 * `connect-src 'self'` covers it). `metadata: false`: the archive's metadata (incl. its own
 * attribution HTML) is never read; the style carries a constant attribution instead.
 */
import type { AddProtocolAction } from "maplibre-gl";
import { Protocol } from "pmtiles";

let registered = false;

export function ensurePmtilesProtocol(maplibre: { addProtocol: (name: string, loadFn: AddProtocolAction) => void }): void {
  if (registered) return;
  const protocol = new Protocol({ metadata: false });
  // pmtiles types its handler for both the MapLibre 3 (callback) and ≥ 4 (promise) protocol APIs;
  // MapLibre 6 calls the promise form with an AbortController.
  maplibre.addProtocol("pmtiles", protocol.tile as unknown as AddProtocolAction);
  registered = true;
}
