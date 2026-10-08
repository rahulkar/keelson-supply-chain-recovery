"use client";

import { Badge, Button, Field, Textarea } from "@keelson/ui";
import { CircleAlert, CircleCheck, LoaderCircle, Play, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { adminError } from "@/lib/admin/format";
import {
  PLAYGROUND_IDLE,
  PLAYGROUND_MAX_QUESTION_CHARS,
  playgroundReduce,
  playgroundResultSchema,
  playgroundUnavailable,
  settlePlayground,
  startPlayground,
  usageLine,
  type PlaygroundState,
} from "@/lib/admin/playground";
import type { CustomSpecialist } from "@/lib/admin/specialists";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";

const ICON = {
  running: <LoaderCircle aria-hidden="true" className="size-4 animate-spin text-fg-muted motion-reduce:animate-none" />,
  done: <CircleCheck aria-hidden="true" className="size-4 text-success" />,
  error: <CircleAlert aria-hidden="true" className="size-4 text-danger" />,
};

/**
 * Agent playground (`docs/contracts/110` §3): one investigator + structurer turn of a saved
 * (slug) or draft specialist on a scratch case — tenant-scoped, budget-capped, never commits.
 * Activity streams in; the findings are shown as claims with their confidence.
 */
export function PlaygroundPanel({ specialist, name, disabledReason }: { specialist: CustomSpecialist | string | null; name: string; disabledReason?: string | null | undefined }) {
  const admin = useAdminApi();
  const [question, setQuestion] = useState("");
  const [state, setState] = useState<PlaygroundState>(PLAYGROUND_IDLE);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);

  const status = useResource(() => admin.playgroundStatus(), "playground-status");
  const unavailable = playgroundUnavailable(status.data);
  const running = state.status === "running";
  const q = question.trim();
  const blocked = unavailable ?? disabledReason ?? (specialist === null ? "Choose a specialist." : null);

  const run = async () => {
    if (!q || blocked || running) return;
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    setState(startPlayground());
    try {
      const out = await admin.playground({ specialist, question: q }, (frame) => setState((s) => playgroundReduce(s, frame)), ctrl.signal);
      if (!out.streamed) {
        const r = playgroundResultSchema.safeParse(out.json);
        setState((s) => (r.success ? playgroundReduce(s, r.data) : { ...s, status: "error", error: "The server sent an unexpected response." }));
      }
      setState((s) => settlePlayground(s));
    } catch (err) {
      if (ctrl.signal.aborted) {
        setState((s) => ({ ...settlePlayground(s), error: "Stopped." }));
        return;
      }
      setState((s) => ({ ...s, status: "error", error: adminError(err), activities: s.activities.map((a) => (a.state === "running" ? { ...a, state: "error" } : a)) }));
    }
  };

  const result = state.result;
  return (
    <div className="flex flex-col gap-3" data-playground="" data-playground-unavailable={unavailable ? "" : undefined}>
      <Field
        label={`Ask ${name}`}
        withCounter
        description="Runs one investigation turn on a scratch case: real read tools, a capped budget, nothing is planned or changed."
      >
        <Textarea value={question} maxLength={PLAYGROUND_MAX_QUESTION_CHARS} rows={2} placeholder="e.g. Which suppliers of trims for style ST-1001 are late this month?" onChange={setQuestion} />
      </Field>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="primary" icon={<Play className="size-4" />} disabled={!q || blocked !== null || running} loading={running} onClick={() => void run()}>
          Run in playground
        </Button>
        {running ? (
          <Button size="sm" variant="ghost" icon={<Square className="size-4" />} onClick={() => abort.current?.abort()}>
            Stop
          </Button>
        ) : null}
        {blocked ? <span className="text-caption text-fg-muted">{blocked}</span> : null}
      </div>

      {state.status !== "idle" ? (
        <section aria-label="Playground activity" className="flex flex-col gap-2 rounded-sm border border-border p-3" aria-busy={running || undefined}>
          <p className="flex flex-wrap items-center gap-2 text-ui font-medium text-fg">
            {state.agent ?? name}
            <Badge size="sm" tone={running ? "info" : state.status === "done" ? "success" : "danger"}>
              {running ? "Working" : state.status === "done" ? "Finished" : "Stopped"}
            </Badge>
            <Badge size="sm" tone="neutral">
              Not committed
            </Badge>
          </p>
          {state.activities.length ? (
            <ol aria-label="Tool calls" className="flex flex-col gap-1" aria-live="polite">
              {state.activities.map((a) => (
                <li key={a.id} className="flex items-center gap-2 text-ui">
                  {ICON[a.state]}
                  <span>{a.label}</span>
                </li>
              ))}
            </ol>
          ) : running ? (
            <p className="text-ui text-fg-muted">Starting…</p>
          ) : null}
          {state.reasoning && running ? <p className="line-clamp-3 text-caption text-fg-muted">{state.reasoning}</p> : null}
          {state.error ? (
            <p role="alert" className="text-ui text-danger">
              {state.error}
            </p>
          ) : null}
          {result ? (
            <div className="flex flex-col gap-2 border-t border-border pt-2">
              <p className="text-caption text-fg-muted">{usageLine(result)}</p>
              {result.tool_calls.some((t) => t.quarantined) ? (
                <p className="text-caption text-fg-muted">Untrusted text was read through the quarantine: only its structured extraction reached the specialist.</p>
              ) : null}
              {result.findings ? (
                <>
                  <h3 className="text-ui font-semibold text-fg">Findings</h3>
                  {result.findings.claims.length ? (
                    <ul className="flex flex-col gap-1">
                      {result.findings.claims.map((c, i) => (
                        <li key={i} className="flex flex-wrap items-start gap-2 text-ui">
                          <Badge size="sm" tone={c.kind === "fact" ? "success" : "info"}>
                            {c.kind === "fact" ? "Fact" : "Inference"} · {c.confidence}
                          </Badge>
                          <span className="min-w-0 flex-1">{c.text}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-ui text-fg-muted">No claims.</p>
                  )}
                  {result.findings.gaps.length ? (
                    <div>
                      <p className="text-caption font-medium text-fg-muted">Gaps</p>
                      <ul className="list-disc pl-5 text-ui">
                        {result.findings.gaps.map((g, i) => (
                          <li key={i}>{g}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </>
              ) : null}
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
