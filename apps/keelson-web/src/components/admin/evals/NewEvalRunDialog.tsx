"use client";

import { CONFIRM_LIVE_TEXT, isLiveMode, type EvalMode } from "@keelson/contracts";
import { Button, Checkbox, ConfirmDialog, Field, TextInput } from "@keelson/ui";
import { useId, useState } from "react";
import { MODE_META, newRunRequest, tokensText, usdText, type EvalRun, type EvalStatusData, type NewRunDraft } from "@/lib/admin/evalRuns";
import { adminError } from "@/lib/admin/format";
import { useAdminApi } from "@/lib/admin/useAdmin";
import { InlineError } from "../common";

type SuiteCase = NonNullable<EvalStatusData["suite"]>["cases"][number];

function group(c: SuiteCase): string {
  if (c.tags.includes("adversarial")) return "Adversarial";
  if (c.tags.includes("golden")) return "Golden";
  return "Other";
}

/**
 * Start an in-app run. Replay (the default) answers from recordings only and never calls the model
 * provider; live modes need the typed confirmation and a budget within the installation's caps.
 * Every case resets Merxa's demo data, which the admin acknowledges.
 */
export function NewEvalRunDialog({
  open,
  onOpenChange,
  status,
  lastLiveCostPerCase,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  status: EvalStatusData;
  lastLiveCostPerCase: number | null;
  onCreated: (run: EvalRun) => void;
}) {
  const admin = useAdminApi();
  const cases = status.suite?.cases ?? [];
  const allIds = cases.map((c) => c.id);
  const firstAvailable = status.modes.find((m) => m.available)?.mode ?? "replay";
  const [draft, setDraft] = useState<NewRunDraft>({
    mode: firstAvailable, caseIds: allIds, acknowledged: false, confirm: "", maxTokens: "", maxCostUsd: "", note: "",
  });
  const [error, setError] = useState<string | null>(null);
  const problemId = useId();
  const set = (patch: Partial<NewRunDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const { body, problem } = newRunRequest(draft, status.live_caps, allIds);
  const live = isLiveMode(draft.mode);
  const groups = [...new Set(cases.map(group))].sort((a, b) => a.localeCompare(b));
  const estimate = live && lastLiveCostPerCase != null ? lastLiveCostPerCase * draft.caseIds.length : null;

  const submit = async () => {
    if (!body) return false;
    try {
      const run = await admin.createEvalRun(body);
      setError(null);
      onCreated(run);
      return true;
    } catch (err) {
      setError(adminError(err));
      return false;
    }
  };

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title="New eval run"
      description="The runner plays each case against this stack as the demo personas, like the command line."
      confirmLabel={live ? "Start live run" : "Start replay run"}
      tone={live ? "danger" : "default"}
      confirmDisabled={body === null}
      onConfirm={submit}
    >
      <div className="flex flex-col gap-4">
        {error ? <InlineError message={error} /> : null}
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-ui font-medium text-fg">Mode</legend>
          {status.modes.map((m) => {
            const id = `eval-mode-${m.mode}`;
            return (
              <div key={m.mode} className="flex items-start gap-2">
                <input
                  id={id}
                  type="radio"
                  name="eval-mode"
                  className="mt-1 accent-accent"
                  checked={draft.mode === m.mode}
                  disabled={!m.available}
                  aria-describedby={`${id}-d`}
                  onChange={() => set({ mode: m.mode as EvalMode, confirm: "" })}
                />
                <label htmlFor={id} className="flex flex-col">
                  <span className="text-ui font-medium text-fg">{MODE_META[m.mode].label}</span>
                  <span id={`${id}-d`} className="text-caption text-fg-muted">
                    {m.available ? MODE_META[m.mode].description : `Unavailable: ${m.message ?? m.reason ?? "not possible now"}`}
                  </span>
                </label>
              </div>
            );
          })}
        </fieldset>

        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 flex w-full items-center gap-2 text-ui font-medium text-fg">
            Cases <span className="font-normal text-fg-muted">· {draft.caseIds.length} of {allIds.length}</span>
          </legend>
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" onClick={() => set({ caseIds: allIds })}>
              Select all
            </Button>
            <Button size="sm" variant="ghost" onClick={() => set({ caseIds: [] })}>
              Clear
            </Button>
          </div>
          {groups.map((g) => (
            <div key={g} className="flex flex-col gap-1.5">
              <p className="text-caption font-medium text-fg-muted">{g}</p>
              {cases
                .filter((c) => group(c) === g)
                .map((c) => (
                  <Checkbox
                    key={c.id}
                    checked={draft.caseIds.includes(c.id)}
                    onCheckedChange={(on) => set({ caseIds: on ? allIds.filter((x) => x === c.id || draft.caseIds.includes(x)) : draft.caseIds.filter((x) => x !== c.id) })}
                    label={c.title}
                    description={
                      <>
                        <span className="font-mono">{c.id}</span>
                        {draft.mode === "replay" && c.needs_live_embeddings ? " · skipped in replay (needs live embeddings)" : ""}
                        {c.injects ? " · injects test data into Merxa" : ""}
                      </>
                    }
                  />
                ))}
            </div>
          ))}
        </fieldset>

        {live ? (
          <div className="flex flex-col gap-3 rounded-sm border border-warning/40 bg-warning-soft/40 p-3">
            <p className="text-ui text-fg">
              A live run calls the model provider and costs money. Caps of this installation: {tokensText(status.live_caps.max_tokens)} tokens and{" "}
              {usdText(status.live_caps.max_cost_microusd)} per run.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Max tokens" description="Live tokens over every case of the run.">
                <TextInput inputMode="numeric" value={draft.maxTokens} onChange={(e) => set({ maxTokens: e.target.value })} />
              </Field>
              <Field label="Max cost (USD)" description="Whole run, in dollars (LiteLLM prices).">
                <TextInput inputMode="decimal" value={draft.maxCostUsd} onChange={(e) => set({ maxCostUsd: e.target.value })} />
              </Field>
            </div>
            <p className="text-caption text-fg-muted">
              {estimate != null
                ? `The last live run cost ${usdText(lastLiveCostPerCase)} per case: about ${usdText(estimate)} for ${draft.caseIds.length} case(s).`
                : "No earlier live run to estimate the cost from."}
            </p>
            <Field label={`Type ${CONFIRM_LIVE_TEXT} to confirm`}>
              <TextInput autoComplete="off" value={draft.confirm} onChange={(e) => set({ confirm: e.target.value })} />
            </Field>
          </div>
        ) : null}

        <Checkbox
          checked={draft.acknowledged}
          onCheckedChange={(on) => set({ acknowledged: on })}
          label="Each case resets Merxa's demo data"
          description="Never start a run while someone is presenting from this stack."
        />
        <Field label="Note (optional)">
          <TextInput maxLength={200} value={draft.note} onChange={(e) => set({ note: e.target.value })} />
        </Field>
        <p id={problemId} aria-live="polite" className="text-caption text-fg-muted">
          {problem ?? (live ? "Ready to start a live run." : "Ready: no model provider calls.")}
        </p>
      </div>
    </ConfirmDialog>
  );
}
