"use client";

import type { BlockEnvelope } from "@keelson/contracts";
import { BlockRenderer, type BlockAction, type BlockDecision, type BlockDensity, type BlockViewer } from "@keelson/viz";
import { useMemo } from "react";
import { createBlockDataLoader } from "@/lib/blocks/dataLoader";
import "@/lib/blocks/canvasBlocks";
import { useApi } from "@/lib/client/session";

/**
 * One block of the case thread / canvas. `data_ref` blocks carry their large payload out of
 * band (`GET /api/blocks/{id}/data`): `BlockRenderer` resolves it with the app's loader before
 * the block renders (loading placeholder, retryable error card, payload validated per
 * `kind@v`). A re-emitted block (`revision`) fetches its payload again. The M3 canvas visuals
 * are registered as `next/dynamic` client-only components (`@/lib/blocks/canvasBlocks`).
 */
export function CaseBlock({
  envelope,
  revision,
  onAction,
  busy,
  density,
  viewer,
  decision,
}: {
  envelope: BlockEnvelope;
  revision: number;
  onAction: (a: BlockAction) => Promise<void> | void;
  busy: boolean;
  density: BlockDensity;
  viewer?: BlockViewer | undefined;
  /** Approach / approval card that is no longer waiting: show who decided instead of buttons. */
  decision?: BlockDecision | undefined;
}) {
  const api = useApi();
  const loadData = useMemo(() => createBlockDataLoader(api), [api]);
  return (
    <div data-block-id={envelope.id} id={`block-${envelope.id}`} tabIndex={-1} className="scroll-mt-4 rounded-lg outline-none">
      <BlockRenderer
        envelope={envelope}
        onAction={onAction}
        busy={busy}
        density={density}
        viewer={viewer}
        decision={decision}
        loadData={loadData}
        dataKey={revision}
      />
    </div>
  );
}
