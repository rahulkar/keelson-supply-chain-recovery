"use client";

import { Badge } from "@keelson/ui";
import { statusMeta, type NodeStatus } from "./status";

/** Status label (text carries the meaning; the dot only echoes the map / graph colour). */
export function StatusBadge({ status, size = "sm" }: { status: NodeStatus; size?: "sm" | "md" }) {
  const meta = statusMeta[status];
  return (
    <Badge tone={meta.tone} variant={meta.variant} size={size} dot title={meta.description}>
      {meta.label}
    </Badge>
  );
}
