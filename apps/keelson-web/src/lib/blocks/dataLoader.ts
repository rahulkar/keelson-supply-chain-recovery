import type { BlockEnvelope } from "@keelson/contracts";
import type { BlockDataLoader } from "@keelson/viz";
import type { ApiClient } from "@/lib/api/client";

/**
 * Loads a block's `data_ref` payload through the app's API client (same-origin, session cookie,
 * 401 → sign-in). Always the block's own endpoint `GET /api/blocks/{id}/data` — the envelope's
 * `data_ref` string is never fetched as a URL. `BlockRenderer` validates the payload per
 * `kind@v` (`parseBlockData`) before the block renders.
 */
export function createBlockDataLoader(api: Pick<ApiClient, "blockData">): BlockDataLoader {
  return (envelope: BlockEnvelope, { signal }) => api.blockData(envelope.id, { signal });
}
