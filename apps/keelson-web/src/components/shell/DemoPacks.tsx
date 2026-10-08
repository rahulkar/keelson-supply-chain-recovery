"use client";

import { Button, Dialog, Field, Spinner, TextInput, toast } from "@keelson/ui";
import { Play, RotateCcw, SkipForward } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError, describeError } from "@/lib/api/client";
import { AlertsApi } from "@/lib/alerts/client";
import { PACK_ID_PATTERN, type DemoPack, type DemoPackAction, type DemoPackResult } from "@/lib/alerts/types";
import { useSession } from "@/lib/client/session";

type Load = { status: "loading" } | { status: "error"; message: string; noList: boolean } | { status: "ready"; packs: DemoPack[] };
type Settled = { attempt: number } & ({ status: "error"; message: string; noList: boolean } | { status: "ready"; packs: DemoPack[] });

/** The deployment has no pack list route (`GET /api/demo/packs` → 404/405): enter an id instead. */
function isMissingRoute(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 405);
}

/**
 * Fallback when the engine cannot list packs: the presenter types the pack id (the folder name
 * under `data/scenarios/`, e.g. from the runbook); only slugs are accepted.
 */
function PackIdForm({ onUse }: { onUse: (id: string) => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const id = value.trim().toLowerCase();
    if (!PACK_ID_PATTERN.test(id)) {
      setError("Use the scenario's id: lowercase letters, digits and hyphens.");
      return;
    }
    setError(null);
    onUse(id);
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
      <Field label="Scenario id" error={error ?? undefined} className="min-w-48 flex-1">
        <TextInput value={value} onChange={(e) => setValue(e.target.value)} autoComplete="off" spellCheck={false} mono />
      </Field>
      <Button type="submit" size="sm">
        Use scenario
      </Button>
    </form>
  );
}

/** "Step 2 of 4" when the engine says where the replay is. */
export function packProgress(p: { step?: number | null | undefined; steps_total?: number | null | undefined; status?: string | null | undefined }): string | null {
  const parts: string[] = [];
  if (p.status) parts.push(p.status.replace(/[_-]+/g, " ").replace(/^\w/, (c) => c.toUpperCase()));
  if (typeof p.step === "number" && typeof p.steps_total === "number" && p.steps_total > 0) parts.push(`step ${p.step} of ${p.steps_total}`);
  else if (typeof p.step === "number") parts.push(`step ${p.step}`);
  return parts.length ? parts.join(" · ") : null;
}

const DONE: Record<DemoPackAction, string> = { start: "Started", advance: "Advanced", reset: "Reset" };

/**
 * DEMO presenter controls for scenario packs (M6, `docs/contracts/100` §7): list
 * (`GET /api/demo/packs`) and Start / Advance / Reset (`POST /api/demo/packs/{pack}/…`, engine
 * proxy to the Signal Hub). Pack payloads are simulated; resulting alerts carry the DEMO badge.
 */
export function DemoPacksDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Demo scenarios"
      description="Replays recorded hazard, news and supplier updates through the real feeds. Everything they create is labelled DEMO."
    >
      <DemoPacksPanel active={open} />
    </Dialog>
  );
}

/**
 * The pack list with Start / Next step / Reset (also on the admin Signals page, M7 §5). Loads
 * while `active`.
 */
export function DemoPacksPanel({ active }: { active: boolean }) {
  const open = active;
  const { api: client } = useSession();
  const api = useMemo(() => new AlertsApi(client), [client]);
  const [settled, setSettled] = useState<Settled | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api
      .demoPacks()
      .then((packs) => {
        if (!cancelled) setSettled({ attempt, status: "ready", packs });
      })
      .catch((err: unknown) => {
        if (!cancelled) setSettled({ attempt, status: "error", message: describeError(err), noList: isMissingRoute(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [api, open, attempt]);
  // Re-opening shows the last list while it refreshes; "Try again" shows the spinner.
  const load: Load = !settled || (settled.attempt !== attempt && settled.status === "error") ? { status: "loading" } : settled;

  const run = async (pack: DemoPack, action: DemoPackAction) => {
    if (busy) return;
    setBusy(`${pack.id}:${action}`);
    try {
      const result: DemoPackResult = await api.packAction(pack.id, action);
      const progress = packProgress(result);
      const description = [progress, result.note].filter(Boolean).join(" · ");
      toast.success(`${DONE[action]} "${pack.title}"`, { description: description || undefined });
      setSettled((prev) =>
        prev && prev.status === "ready"
          ? { ...prev, packs: prev.packs.map((p) => (p.id === pack.id ? { ...p, status: result.status ?? p.status, step: result.step ?? p.step, steps_total: result.steps_total ?? p.steps_total } : p)) }
          : prev,
      );
    } catch (err) {
      toast.error(`Couldn't ${action} "${pack.title}"`, { description: describeError(err) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      {load.status === "loading" ? (
        <p role="status" className="flex items-center gap-2 text-ui text-fg-muted">
          <Spinner size={14} /> Loading scenarios…
        </p>
      ) : load.status === "error" && load.noList ? (
        <div className="flex flex-col gap-3">
          <p className="text-ui text-fg-muted">This deployment can&apos;t list the installed scenarios. Enter a scenario&apos;s id to control it.</p>
          <PackIdForm onUse={(id) => setSettled({ attempt, status: "ready", packs: [{ id, title: id, step: null, steps_total: null }] })} />
        </div>
      ) : load.status === "error" ? (
        <div className="flex flex-col items-start gap-2">
          <p className="text-ui text-fg">Scenarios are unavailable. {load.message}</p>
          <Button size="sm" onClick={() => setAttempt((n) => n + 1)}>
            Try again
          </Button>
        </div>
      ) : load.packs.length === 0 ? (
        <p className="text-ui text-fg-muted">No demo scenarios are installed.</p>
      ) : (
        <ul aria-label="Demo scenarios" className="flex flex-col divide-y divide-border">
          {load.packs.map((p) => {
            const progress = packProgress(p);
            return (
              <li key={p.id} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
                <div className="flex flex-col gap-0.5">
                  <span className="text-ui font-medium text-fg">{p.title}</span>
                  {p.description ? <span className="text-caption text-fg-muted">{p.description}</span> : null}
                  {progress ? <span className="text-caption text-fg-subtle">{progress}</span> : null}
                </div>
                <div role="group" aria-label={`Controls for ${p.title}`} className="flex flex-wrap gap-2">
                  {(
                    [
                      ["start", "Start", <Play key="i" aria-hidden="true" />],
                      ["advance", "Next step", <SkipForward key="i" aria-hidden="true" />],
                      ["reset", "Reset", <RotateCcw key="i" aria-hidden="true" />],
                    ] as const
                  ).map(([action, label, icon]) => (
                    <Button
                      key={action}
                      size="sm"
                      variant={action === "start" ? "primary" : "secondary"}
                      icon={icon}
                      loading={busy === `${p.id}:${action}`}
                      disabled={busy !== null}
                      onClick={() => void run(p, action)}
                      aria-label={`${label}: ${p.title}`}
                    >
                      {label}
                    </Button>
                  ))}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
