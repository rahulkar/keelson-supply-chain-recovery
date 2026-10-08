"use client";

import { Drawer } from "@keelson/ui";
import { PlanVersionSwitcher, type PlanVersionView } from "@keelson/viz";

/**
 * Plan version switcher (M5 re-plan, `docs/contracts/95` §4/§7): v1 ↔ v2 with each version's
 * status, its "What changed" line, the comparison against the version before and its plan diff.
 * Built from the case's own blocks (`planVersionViews`), so nothing extra is fetched.
 */
export function PlanVersionsDialog({
  open,
  onOpenChange,
  versions,
  initialVersion,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  versions: readonly PlanVersionView[];
  initialVersion?: number | undefined;
}) {
  return (
    <Drawer open={open} onOpenChange={onOpenChange} size="lg" title="Plan versions" description="Compare how the plan changed between versions.">
      {open ? <PlanVersionSwitcher key={initialVersion ?? "latest"} versions={versions} initialVersion={initialVersion} /> : null}
    </Drawer>
  );
}
