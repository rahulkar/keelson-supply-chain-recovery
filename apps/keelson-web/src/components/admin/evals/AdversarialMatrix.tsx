"use client";

import { Badge, type BadgeTone } from "@keelson/ui";
import { ADVERSARIAL_CHECKS, adversarialCells, type CheckCell, type EvalCaseResult } from "@/lib/admin/evalRuns";

const CELL: Record<CheckCell, { label: string; tone: BadgeTone }> = {
  ok: { label: "Held", tone: "success" },
  fail: { label: "Failed", tone: "danger" },
  soft: { label: "Failed (soft)", tone: "warning" },
  none: { label: "Not checked", tone: "neutral" },
};

/** Adversarial cases × the case-level safety checks (quarantine, no writes, no commit, no leak). */
export function AdversarialMatrix({ results }: { results: readonly EvalCaseResult[] }) {
  if (!results.length) return null;
  return (
    <div role="group" aria-label="Adversarial outcomes" tabIndex={0} className="overflow-x-auto rounded-md border border-border">
      <table className="w-full text-caption">
        <caption className="sr-only">Adversarial outcomes</caption>
        <thead>
          <tr className="border-b border-border">
            <th scope="col" className="px-3 py-1.5 text-left font-medium text-fg-muted">
              Case
            </th>
            {ADVERSARIAL_CHECKS.map((c) => (
              <th key={c.key} scope="col" className="px-2 py-1.5 text-left font-medium text-fg-muted">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {results.map((r) => {
            const cells = adversarialCells(r);
            return (
              <tr key={r.case_id} className="border-b border-border last:border-0">
                <th scope="row" className="px-3 py-1.5 text-left font-mono font-normal text-fg">
                  {r.case_id}
                </th>
                {ADVERSARIAL_CHECKS.map((c) => (
                  <td key={c.key} className="px-2 py-1.5">
                    <Badge size="sm" tone={CELL[cells[c.key]].tone}>
                      {CELL[cells[c.key]].label}
                    </Badge>
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
