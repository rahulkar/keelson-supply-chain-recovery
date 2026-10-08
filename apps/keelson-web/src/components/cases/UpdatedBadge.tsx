"use client";

import { Badge } from "@keelson/ui";
import { useSyncExternalStore } from "react";
import { caseSeenAt, isUpdatedSince } from "@/lib/seen";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}

/**
 * "Updated" when the case changed after this viewer last opened it. `localStorage` is read on the
 * client only (the server snapshot is "never seen"), so hydration never depends on it.
 */
export function UpdatedBadge({ caseId, updatedAt }: { caseId: string; updatedAt: string | null }) {
  const seenAt = useSyncExternalStore(
    subscribe,
    () => caseSeenAt(caseId),
    () => null,
  );
  return isUpdatedSince(updatedAt, seenAt) ? (
    <Badge tone="info" dot>
      Updated
    </Badge>
  ) : null;
}
