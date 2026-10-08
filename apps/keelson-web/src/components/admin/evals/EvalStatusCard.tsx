"use client";

import { Badge } from "@keelson/ui";
import { AlertTriangle } from "lucide-react";
import { MODE_META, RUNNER_START_HINT, tokensText, usdText, type EvalStatusData } from "@/lib/admin/evalRuns";
import { int, when } from "@/lib/admin/format";
import { AdminSection, Facts } from "../common";

/** Runner, suite, what each mode can do now, and the recordings a replay run answers from. */
export function EvalStatusCard({ status }: { status: EvalStatusData }) {
  const { runner, suite, recordings } = status;
  const replay = status.modes.find((m) => m.mode === "replay");
  const liveOff = status.modes.filter((m) => m.mode !== "replay" && !m.available);
  const agents = Object.entries(recordings.agents).sort((a, b) => b[1] - a[1]);
  return (
    <AdminSection
      id="eval-status"
      title="Runner and recordings"
      description="The in-app runner drives the eval cases black-box over HTTP as the demo personas, like the command line."
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge size="sm" tone={runner.online ? "success" : runner.configured ? "warning" : "neutral"} dot>
          {runner.online ? "Runner online" : runner.configured ? "Runner offline" : "Runner not configured"}
        </Badge>
        <Badge size="sm" variant="outline">
          Engine mode: {status.process_replay_mode}
        </Badge>
      </div>
      {!runner.online && runner.configured ? (
        <p className="text-ui text-fg-muted">
          Start it with <code className="rounded-sm bg-surface-2 px-1 font-mono text-caption break-all">{RUNNER_START_HINT}</code>.
        </p>
      ) : null}
      {replay && !replay.available && replay.reason !== "runner_offline" && replay.message ? <p className="text-ui text-fg-muted">{replay.message}</p> : null}
      <Facts
        items={[
          ["Tenant", <span key="t" className="font-mono">{status.tenant}</span>],
          ["Cases in the suite", suite ? `${suite.cases.length} (suite ${suite.suite_hash.slice(0, 12)})` : "Not reported yet"],
          ["Last heartbeat", when(runner.last_seen_at)],
          ["Live runs", liveOff.length ? (liveOff[0]?.message ?? "Unavailable") : `Allowed up to ${tokensText(status.live_caps.max_tokens)} tokens and ${usdText(status.live_caps.max_cost_microusd)} per run`],
          ["Recordings", recordings.rows ? `${int(recordings.rows)} model answers (${int(Math.round(recordings.bytes / 1024))} KB), newest ${when(recordings.newest_at)}` : "None"],
        ]}
      />
      {recordings.rows === 0 ? (
        <p role="note" className="flex items-start gap-2 rounded-sm border border-warning/40 bg-warning-soft px-3 py-2 text-ui text-fg">
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
          <span>
            There are no recorded model answers for {status.tenant}: replay runs will stop at the first model call ({MODE_META.replay.label.toLowerCase()} never calls the provider).
          </span>
        </p>
      ) : agents.length ? (
        <p className="text-caption text-fg-muted">
          By agent: {agents.slice(0, 8).map(([a, n]) => `${a} ${int(n)}`).join(" · ")}
          {agents.length > 8 ? " …" : ""}
        </p>
      ) : null}
    </AdminSection>
  );
}
