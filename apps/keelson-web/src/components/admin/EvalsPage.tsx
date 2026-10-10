"use client";

import { Button } from "@keelson/ui";
import { FileUp, Play, RotateCw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { isActive, lastLiveCostPerCase } from "@/lib/admin/evalRuns";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminPageHeader, ResourceView } from "./common";
import { EvalProgress } from "./evals/EvalProgress";
import { EvalResultsFolder } from "./evals/EvalResultsFolder";
import { EvalRunsTable } from "./evals/EvalRunsTable";
import { EvalStatusCard } from "./evals/EvalStatusCard";
import { EvalTrends } from "./evals/EvalTrends";
import { ImportEvalsDialog } from "./evals/ImportEvalsDialog";
import { NewEvalRunDialog } from "./evals/NewEvalRunDialog";
import { evalsHref } from "./evals/common";

/**
 * **Evals** (`docs/contracts/128`): start eval runs in the app (replay by default: recorded model
 * answers only, no provider calls), follow the active run, track trends against targets, compare
 * runs and import the command line's results. Platform admins only (the route gates it).
 */
export function EvalsPage() {
  const admin = useAdminApi();
  const router = useRouter();
  const [epoch, setEpoch] = useState(0);
  const status = useResource(() => admin.evalsStatus(), `evals-status:${epoch}`);
  const runs = useResource(() => admin.evalRuns({ limit: 20 }), `eval-runs:${epoch}`);
  const [newOpen, setNewOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const refresh = () => setEpoch((n) => n + 1);
  const active = status.data?.active_run ?? null;
  const canStart = !!status.data && status.data.modes.some((m) => m.available) && !active && !!status.data.suite;

  return (
    <>
      <AdminPageHeader
        title="Evals"
        description="Golden and adversarial cases played against this stack. Replay runs answer from recorded model calls only; live runs need a typed confirmation and a budget."
        actions={
          <>
            <Button size="sm" variant="primary" icon={<Play className="size-4" />} disabled={!canStart} onClick={() => setNewOpen(true)}>
              New run
            </Button>
            <Button size="sm" icon={<FileUp className="size-4" />} onClick={() => setImportOpen(true)}>
              Import
            </Button>
            <Button size="sm" variant="secondary" icon={<RotateCw className="size-4" />} loading={status.loading && status.data !== undefined} onClick={refresh}>
              Refresh
            </Button>
          </>
        }
      />
      <ResourceView resource={status} label="the eval status">
        {(s) => (
          <>
            <EvalStatusCard status={s} />
            {s.active_run && isActive(s.active_run.status) ? <EvalProgress key={s.active_run.id} run={s.active_run} onChanged={refresh} /> : null}
            {newOpen ? (
              <NewEvalRunDialog
                open
                onOpenChange={setNewOpen}
                status={s}
                lastLiveCostPerCase={lastLiveCostPerCase(runs.data?.items ?? [])}
                onCreated={(run) => {
                  refresh();
                  router.push(evalsHref.run(run.id));
                }}
              />
            ) : null}
          </>
        )}
      </ResourceView>
      <EvalTrends refreshKey={String(epoch)} />
      <ResourceView resource={runs} label="eval runs">
        {(list) => <EvalRunsTable key={`${epoch}`} list={list} />}
      </ResourceView>
      <EvalResultsFolder refreshKey={String(epoch)} onImported={refresh} />
      <ImportEvalsDialog open={importOpen} onOpenChange={setImportOpen} onImported={refresh} />
    </>
  );
}
