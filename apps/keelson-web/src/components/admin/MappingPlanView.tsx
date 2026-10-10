"use client";

import type { MappingPlan } from "@keelson/contracts";
import { Badge } from "@keelson/ui";
import { sourceLabel } from "@/lib/admin/proposals";

type Plan = MappingPlan;
type PlanSource = Plan["sources"][number];

function pagingText(s: PlanSource["source"]): string {
  if (!s.page_param) return "one call";
  const how = s.cursor_path ? `cursor ${s.page_param} from ${s.cursor_path}` : `pages via ${s.page_param}`;
  return `${how} · ${s.page_size} per page · up to ${s.max_pages} ${s.max_pages === 1 ? "page" : "pages"}`;
}

/**
 * **Explain** (`POST …/mappings/read/compile`, `docs/contracts/121` §2): what the DuckDB mapping
 * compiler makes of a read mapping — each distinct read is fetched once (its users listed), lookups
 * are built first, then per entity mapping its joins, grouping and the expression, target type and
 * generated SQL of every column (SQL on demand), and the checks run before anything is loaded.
 */
export function MappingPlanView({ plan, draft }: { plan: Plan; draft: boolean }) {
  const uses = plan.sources.reduce((n, s) => n + s.used_by.length, 0);
  const keyLabel = new Map(plan.sources.map((s) => [s.key, sourceLabel(s.source)]));
  return (
    <div className="flex flex-col gap-3 rounded-sm border border-border p-3" data-mapping-plan="" aria-live="polite">
      <p className="flex flex-wrap items-center gap-2 text-ui">
        <Badge size="sm" tone="info">
          Compiled
        </Badge>
        <span className="text-fg-muted">
          {draft ? "Unsaved draft" : "Saved mapping"}: {plan.sources.length} distinct {plan.sources.length === 1 ? "read" : "reads"} for {uses}{" "}
          {uses === 1 ? "use" : "uses"} (each read is fetched once) · nothing was called
        </span>
      </p>
      {plan.warnings.length ? (
        <ul className="list-disc pl-5 text-caption text-warning" aria-label="Compiler warnings">
          {plan.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}
      <div className="max-w-full overflow-x-auto">
        <table className="min-w-full text-caption">
          <caption className="sr-only">Reads the crawl makes</caption>
          <thead>
            <tr className="text-left text-fg-muted">
              <th scope="col" className="px-2 py-1 font-medium">Read</th>
              <th scope="col" className="px-2 py-1 font-medium">Paging</th>
              <th scope="col" className="px-2 py-1 font-medium">Used by</th>
              <th scope="col" className="px-2 py-1 font-medium">Fields read</th>
            </tr>
          </thead>
          <tbody>
            {plan.sources.map((s) => (
              <tr key={s.key} className="border-t border-border" data-plan-source={s.key}>
                <td className="px-2 py-1 font-mono break-all">{sourceLabel(s.source)}</td>
                <td className="px-2 py-1">{pagingText(s.source)}</td>
                <td className="px-2 py-1 font-mono">{s.used_by.join(", ")}</td>
                <td className="px-2 py-1">{s.paths.length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {plan.lookups.length ? (
        <ul className="flex flex-col gap-0.5 text-caption" aria-label="Lookups (built before the entities)">
          {plan.lookups.map((lk) => (
            <li key={lk.name}>
              <span className="font-mono">{lk.name}</span> · {lk.kind}
              {lk.kind === "static" ? ` · ${lk.entries ?? 0} entries` : ` · key ${lk.key ?? "?"}${lk.source_key ? ` from ${keyLabel.get(lk.source_key) ?? lk.source_key}` : ""}`}
              {lk.paths.length ? ` · reads ${lk.paths.join(", ")}` : ""}
            </li>
          ))}
        </ul>
      ) : null}
      {plan.entities.map((e) => (
        <details key={e.where} className="rounded-sm border border-border px-2 py-1" data-plan-entity={e.where}>
          <summary className="cursor-pointer text-ui">
            <span className="font-mono">{e.where}</span> from <span className="font-mono">{keyLabel.get(e.source_key) ?? e.source_key}</span>
            {e.group_by ? <span className="text-fg-muted"> · one row per {e.group_by.join(", ")}</span> : null}
            {e.dedupe ? <span className="text-fg-muted"> · duplicates collapse</span> : null}
            {e.joins.length ? <span className="text-fg-muted"> · joins {e.joins.map((j) => `${j.table} (${j.kind})`).join(", ")}</span> : null}
          </summary>
          <div className="mt-1 max-w-full overflow-x-auto">
            <table className="min-w-full text-caption">
              <caption className="sr-only">Columns of {e.where}</caption>
              <thead>
                <tr className="text-left text-fg-muted">
                  <th scope="col" className="px-2 py-1 font-medium">Column</th>
                  <th scope="col" className="px-2 py-1 font-medium">Expression</th>
                  <th scope="col" className="px-2 py-1 font-medium">Validated as</th>
                </tr>
              </thead>
              <tbody>
                {e.columns.map((c) => (
                  <tr key={c.column} className="border-t border-border">
                    <td className="px-2 py-1 font-mono">
                      {c.column}
                      {c.required ? <span className="text-fg-muted"> (required)</span> : null}
                    </td>
                    <td className="px-2 py-1 font-mono break-all">{c.expression}</td>
                    <td className="px-2 py-1">{c.target_type}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details className="mt-1">
            <summary className="cursor-pointer text-caption text-fg-muted">Show SQL</summary>
            <pre className="max-h-72 overflow-auto rounded-sm bg-surface-2 p-2 font-mono text-[0.6875rem]" aria-label={`Generated SQL of ${e.where}`}>
              {e.sql}
            </pre>
          </details>
        </details>
      ))}
      {plan.checks.length ? (
        <div>
          <p className="text-caption font-medium text-fg-muted">Checked before anything is loaded</p>
          <ul className="flex flex-col gap-0.5 text-caption" aria-label="Checks">
            {plan.checks.map((c) => (
              <li key={`${c.code}:${c.label}`} className="flex flex-wrap items-center gap-2">
                <Badge size="sm" tone={c.severity === "error" ? "danger" : "neutral"}>
                  {c.severity === "error" ? "Stops the crawl" : "Reported"}
                </Badge>
                <span>{c.label}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
