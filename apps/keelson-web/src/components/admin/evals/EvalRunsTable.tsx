"use client";

import { Button, Checkbox, EmptyState, Table } from "@keelson/ui";
import { FlaskConical, GitCompare } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { passedText, tokensText, usdText, type EvalRun, type EvalRunList } from "@/lib/admin/evalRuns";
import { adminError, when } from "@/lib/admin/format";
import { useAdminApi } from "@/lib/admin/useAdmin";
import { AdminSection, InlineError } from "../common";
import { ModeBadge, RunStatusBadge, evalsHref, linkClass } from "./common";

/** Run history, newest first; pick two rows to compare them (older = base). */
export function EvalRunsTable({ list }: { list: EvalRunList }) {
  const admin = useAdminApi();
  const router = useRouter();
  const [extra, setExtra] = useState<EvalRun[]>([]);
  const [next, setNext] = useState<string | null>(list.next_before);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const runs = [...list.items, ...extra.filter((r) => !list.items.some((x) => x.id === r.id))];

  const more = async () => {
    if (!next) return;
    setLoading(true);
    try {
      const page = await admin.evalRuns({ limit: 20, before: next });
      setExtra((prev) => [...prev, ...page.items]);
      setNext(page.next_before);
      setError(null);
    } catch (err) {
      setError(adminError(err));
    } finally {
      setLoading(false);
    }
  };

  const toggle = (id: string, on: boolean) => setPicked((prev) => (on ? [...prev.filter((x) => x !== id), id].slice(-2) : prev.filter((x) => x !== id)));
  const compare = () => {
    const [a, b] = picked.map((id) => runs.find((r) => r.id === id)!).sort((x, y) => (x.started_at ?? x.created_at).localeCompare(y.started_at ?? y.created_at));
    if (a && b) router.push(evalsHref.compare(a.id, b.id));
  };

  return (
    <AdminSection
      id="eval-runs"
      title="Runs"
      description="In-app runs and imported command-line results, newest first. Select two to compare."
      actions={
        <Button size="sm" icon={<GitCompare className="size-4" />} disabled={picked.length !== 2} onClick={compare}>
          Compare selected
        </Button>
      }
    >
      {error ? <InlineError message={error} onRetry={more} /> : null}
      {runs.length === 0 ? (
        <EmptyState icon={<FlaskConical />} title="No eval runs yet" description="Start a run, or import a results file from the command line." />
      ) : (
        <Table<EvalRun>
          caption="Eval runs"
          hideCaption
          rows={runs}
          rowKey={(r) => r.id}
          rowTone={(r) => (r.status === "failed" ? "danger" : r.breaches > 0 ? "warning" : undefined)}
          columns={[
            {
              id: "pick",
              header: "Compare",
              srOnlyHeader: true,
              cell: (r) => (
                <Checkbox
                  checked={picked.includes(r.id)}
                  onCheckedChange={(on) => toggle(r.id, on)}
                  disabled={!r.summary}
                  label={<span className="sr-only">Compare the run of {when(r.started_at ?? r.created_at)}</span>}
                />
              ),
            },
            {
              id: "started",
              header: "Started",
              rowHeader: true,
              nowrap: true,
              cell: (r) => (
                <Link href={evalsHref.run(r.id)} className={linkClass}>
                  {when(r.started_at ?? r.created_at)}
                </Link>
              ),
            },
            { id: "source", header: "Source", nowrap: true, cell: (r) => (r.source === "in_app" ? "In-app" : r.file_name ? `Import · ${r.file_name}` : "Import") },
            { id: "mode", header: "Mode", nowrap: true, cell: (r) => <ModeBadge mode={r.mode} /> },
            { id: "status", header: "Status", nowrap: true, cell: (r) => <RunStatusBadge status={r.status} /> },
            { id: "passed", header: "Passed", nowrap: true, align: "end", cell: (r) => passedText(r) },
            { id: "breaches", header: "Targets missed", nowrap: true, align: "end", cell: (r) => (r.summary ? String(r.breaches) : "—") },
            {
              id: "tokens",
              header: "Tokens",
              nowrap: true,
              align: "end",
              cell: (r) => (r.summary?.tokens_total != null ? `${tokensText(r.summary.tokens_total)}${r.summary.tokens_source === "gateway" ? " ≈" : ""}` : "—"),
            },
            { id: "cost", header: "Cost", nowrap: true, align: "end", cell: (r) => usdText(r.summary?.cost_microusd_total) },
            { id: "by", header: "Requested by", cell: (r) => r.requested_by?.name ?? r.requested_by?.username ?? "—" },
          ]}
        />
      )}
      {next ? (
        <div>
          <Button size="sm" variant="ghost" loading={loading} onClick={() => void more()}>
            Load older runs
          </Button>
        </div>
      ) : null}
    </AdminSection>
  );
}
