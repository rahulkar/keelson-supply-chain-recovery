"use client";

import { Badge, Button, Table, toast } from "@keelson/ui";
import { FolderInput } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { caseScore, evalsResponseSchema } from "@/lib/admin/evals";
import type { EvalFile, FolderImport } from "@/lib/admin/evalRuns";
import { adminError, when } from "@/lib/admin/format";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminSection, ResourceView } from "../common";
import { evalsHref, linkClass } from "./common";

function importToast(out: FolderImport): void {
  const n = out.imported.length;
  const parts = [`${n} run${n === 1 ? "" : "s"} imported`];
  if (out.duplicates) parts.push(`${out.duplicates} already imported`);
  if (out.errors.length) parts.push(`${out.errors.length} unreadable`);
  (out.errors.length ? toast.error : toast.success)(parts.join(" · "), {
    description: out.errors.length ? out.errors.map((e) => `${e.file}: ${e.error}`).join("; ") : undefined,
  });
}

/** Counts of a listed file (the M7 listing shape: per-case counts and scores). */
function fileCounts(f: EvalFile): string {
  const parsed = evalsResponseSchema.safeParse({ items: [f] });
  const run = parsed.success ? parsed.data.runs[0] : undefined;
  if (!run || run.cases.length === 0) return "—";
  const passed = run.cases.filter((c) => c.status === "pass" || c.status === "passed").length;
  const checks = run.cases.reduce((n, c) => n + caseScore(c).total, 0);
  return `${passed} / ${run.cases.length} passed · ${checks} checks`;
}

/** The command line's results folder (mounted read-only into the engine): import its files as runs. */
export function EvalResultsFolder({ onImported, refreshKey }: { onImported: () => void; refreshKey: string }) {
  const admin = useAdminApi();
  const files = useResource(() => admin.evalFiles(), `eval-files:${refreshKey}`);
  const [busy, setBusy] = useState<string | null>(null);

  const run = async (file?: string) => {
    setBusy(file ?? "*");
    try {
      importToast(await admin.importEvalsFolder(file));
      files.reload();
      onImported();
    } catch (err) {
      toast.error(adminError(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <AdminSection
      id="eval-files"
      title="Results folder"
      description="Files the command line wrote to evals/results. Importing one adds it to the run history; the same results are only imported once."
      actions={
        files.data?.directory_available ? (
          <Button size="sm" icon={<FolderInput className="size-4" />} loading={busy === "*"} disabled={busy !== null} onClick={() => void run()}>
            Import all new
          </Button>
        ) : undefined
      }
    >
      <ResourceView resource={files} label="results files">
        {(data) =>
          !data.directory_available ? (
            <p className="text-ui text-fg-muted">The results folder (evals/results) isn&apos;t mounted into the engine.</p>
          ) : (
            <Table<EvalFile>
              caption="Results files"
              hideCaption
              rows={data.items}
              rowKey={(f) => f.file}
              empty="No results files yet."
              columns={[
                { id: "file", header: "File", rowHeader: true, nowrap: true, cell: (f) => <span className="font-mono text-caption">{f.file}</span> },
                { id: "started", header: "Started", nowrap: true, cell: (f) => when(f.started_at) },
                { id: "schema", header: "Format", nowrap: true, cell: (f) => f.schema ?? "—" },
                { id: "cases", header: "Cases", cell: (f) => (f.error ? <span className="text-danger">{f.error}</span> : fileCounts(f)) },
                {
                  id: "action",
                  header: "Import",
                  srOnlyHeader: true,
                  nowrap: true,
                  cell: (f) =>
                    f.imported_run_id ? (
                      <span className="flex items-center gap-2">
                        <Badge size="sm" tone="success">
                          Imported
                        </Badge>
                        <Link href={evalsHref.run(f.imported_run_id)} className={linkClass}>
                          Open run
                        </Link>
                      </span>
                    ) : f.error ? null : (
                      <Button size="sm" variant="ghost" loading={busy === f.file} disabled={busy !== null} onClick={() => void run(f.file)}>
                        Import <span className="sr-only">{f.file}</span>
                      </Button>
                    ),
                },
              ]}
            />
          )
        }
      </ResourceView>
    </AdminSection>
  );
}
