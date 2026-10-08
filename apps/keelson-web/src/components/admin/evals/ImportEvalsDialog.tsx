"use client";

import { ConfirmDialog } from "@keelson/ui";
import Link from "next/link";
import { useId, useState } from "react";
import { EVALS_IMPORT_MAX_BYTES, parseResultsFile, type ImportAnswer } from "@/lib/admin/evalRuns";
import { adminError, int } from "@/lib/admin/format";
import { useAdminApi } from "@/lib/admin/useAdmin";
import { InlineError } from "../common";
import { evalsHref, linkClass } from "./common";

type Picked = { name: string; doc: Record<string, unknown> } | null;

/** Import a results file (`keelson-evals/1` or `/2`, ≤ 8 MiB) from this computer. */
export function ImportEvalsDialog({ open, onOpenChange, onImported }: { open: boolean; onOpenChange: (open: boolean) => void; onImported: () => void }) {
  const admin = useAdminApi();
  const [picked, setPicked] = useState<Picked>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [answer, setAnswer] = useState<ImportAnswer | null>(null);
  const inputId = useId();

  const choose = async (file: File | undefined) => {
    setAnswer(null);
    setPicked(null);
    if (!file) {
      setProblem(null);
      return;
    }
    if (file.size > EVALS_IMPORT_MAX_BYTES) {
      setProblem(`The file is larger than ${int(EVALS_IMPORT_MAX_BYTES / (1024 * 1024))} MiB.`);
      return;
    }
    const parsed = parseResultsFile(await file.text());
    if ("error" in parsed) {
      setProblem(parsed.error);
      return;
    }
    setProblem(null);
    setPicked({ name: file.name, doc: parsed.doc });
  };

  const submit = async () => {
    if (!picked) return false;
    try {
      const out = await admin.importEvals(picked.doc, picked.name);
      setAnswer(out);
      setPicked(null);
      onImported();
    } catch (err) {
      setProblem(adminError(err));
    }
    return false; // stay open: show the outcome (or the error)
  };

  const casesIn = picked && Array.isArray(picked.doc["cases"]) ? (picked.doc["cases"] as unknown[]).length : 0;
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setPicked(null);
          setProblem(null);
          setAnswer(null);
        }
        onOpenChange(next);
      }}
      title="Import eval results"
      description="A results file written by evals/run_live.py (keelson-evals/1 or /2). The same results are only imported once."
      confirmLabel="Import"
      cancelLabel={answer ? "Close" : "Cancel"}
      confirmDisabled={picked === null}
      onConfirm={submit}
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor={inputId} className="text-ui font-medium text-fg">
            Results file
          </label>
          <input
            id={inputId}
            type="file"
            accept=".json,application/json"
            aria-describedby={`${inputId}-d`}
            className="text-ui"
            onChange={(e) => void choose(e.target.files?.[0])}
          />
          <p id={`${inputId}-d`} className="text-caption text-fg-muted">
            JSON, at most 8 MiB.
          </p>
        </div>
        {problem ? <InlineError message={problem} /> : null}
        {picked ? (
          <p className="text-caption text-fg-muted">
            {picked.name}: {String(picked.doc["schema"])} · {casesIn} case(s)
          </p>
        ) : null}
        {answer ? (
          <p role="status" className="text-ui text-fg">
            {answer.duplicate ? "These results were imported before. " : "Imported. "}
            <Link href={evalsHref.run(answer.run_id)} className={linkClass}>
              Open the run
            </Link>
          </p>
        ) : null}
      </div>
    </ConfirmDialog>
  );
}
