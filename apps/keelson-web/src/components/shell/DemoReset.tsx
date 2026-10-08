"use client";

import { Checkbox, ConfirmDialog, toast } from "@keelson/ui";
import { Check, MinusCircle, X } from "lucide-react";
import { useState } from "react";
import { describeError, isApiError } from "@/lib/api/client";
import type { DemoResetResult, DemoResetStep } from "@/lib/api/types";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";

/** Who may reset the demo (engine `DEMO_RESET_ROLES`). */
export const DEMO_RESET_ROLES = ["platform-admin", "demo-presenter"] as const;

export function canResetDemo(me: { demo_mode: boolean; user: { roles: readonly string[] } }): boolean {
  return me.demo_mode && hasAnyRole(me.user, DEMO_RESET_ROLES);
}

const STEP_LABELS: Record<string, string> = {
  merxa_scenario_reset: "Merxa scenario (golden data)",
  keelson_rows: "Cases, plans and changes",
  adk_sessions: "Agent sessions",
  valkey_keys: "Live streams",
  merxa_consents: "Merxa consents",
  token_broker: "Merxa connections",
  agent_spec: "Agent settings",
  sim_clock: "Simulation clock",
  scn_resync: "Supply-chain snapshot",
};

export function resetStepLabel(step: string): string {
  return STEP_LABELS[step] ?? step.replace(/[_.-]+/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

function seconds(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** The step log shown in the toast (state spelled out, never colour alone). */
export function ResetStepList({ steps }: { steps: readonly DemoResetStep[] }) {
  return (
    <ol aria-label="Reset steps" className="mt-1 flex flex-col gap-0.5 text-caption">
      {steps.map((s) => (
        <li key={s.step} data-state={s.status} className="flex items-start gap-1.5">
          <span aria-hidden="true" className="mt-0.5 inline-flex">
            {s.status === "ok" ? (
              <Check className="size-3.5 text-success" />
            ) : s.status === "skipped" ? (
              <MinusCircle className="size-3.5 text-fg-subtle" />
            ) : (
              <X className="size-3.5 text-danger" />
            )}
          </span>
          <span className="min-w-0 flex-1">
            {resetStepLabel(s.step)}: {s.status === "ok" ? "done" : s.status}
            {s.status === "ok" ? ` · ${seconds(s.duration_ms)}` : ""}
            {s.status !== "ok" && s.detail ? ` · ${s.detail}` : ""}
          </span>
        </li>
      ))}
    </ol>
  );
}

/** One toast with the step log of a finished reset. */
export function announceReset(result: DemoResetResult): void {
  const failed = result.steps.filter((s) => s.status === "failed").length;
  const title = result.ok
    ? `Demo reset in ${seconds(result.duration_ms)}`
    : `Demo reset finished with ${failed === 1 ? "1 failed step" : `${failed} failed steps`}`;
  const description = <ResetStepList steps={result.steps} />;
  if (result.ok) toast.success(title, { description, timeout: 10_000 });
  else toast.error(title, { description, timeout: 0 });
}

/**
 * "Reset demo" confirmation (M5, `docs/contracts/95` §6): `POST /api/demo/reset` with optional
 * rehearsal mode (`keep_consents`: Merxa consents and connections are kept), then the step log as
 * a toast and `onDone` (the persona menu sends the presenter home: every case is gone).
 */
export function DemoResetDialog({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (open: boolean) => void; onDone: () => void }) {
  const { api } = useSession();
  const [keepConsents, setKeepConsents] = useState(false);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Reset the demo?"
      description="Deletes every case, plan, change and checklist in the demo workspace, restores Merxa's golden data and the default agent settings. This takes up to 30 seconds."
      confirmLabel="Reset demo"
      tone="danger"
      onConfirm={async () => {
        try {
          const result = await api.demoReset(keepConsents);
          announceReset(result);
          onDone();
          return true;
        } catch (err) {
          toast.error("Couldn't reset the demo", {
            description: isApiError(err, "reset_in_progress") ? "A reset is already running. Try again in a moment." : describeError(err),
          });
          return false;
        }
      }}
    >
      <Checkbox
        checked={keepConsents}
        onCheckedChange={setKeepConsents}
        label="Rehearsal: keep Merxa connections"
        description="Skips revoking the demo users' Merxa consents, so nobody has to connect Merxa again."
      />
    </ConfirmDialog>
  );
}
