import type { Metadata } from "next";
import Link from "next/link";

import { AuditGrid } from "@/components/change-batches/AuditGrid";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { param, type SearchParams } from "@/lib/params";
import { apiGetLatest, load } from "@/lib/server-api";
import type { AuditRow } from "@/lib/types";

export const metadata: Metadata = { title: "Audit Log" };

const ENTITY_TYPES = [
  "change_set",
  "transfer_order",
  "inventory_position",
  "allocation",
  "po_line",
  "purchase_order",
  "supplier_message",
  "ui_session",
  "scenario",
  "sim_clock",
] as const;
const MAX_ROWS = 5000;

type Props = { searchParams: Promise<SearchParams> };

export default async function AuditPage({ searchParams }: Props) {
  const sp = await searchParams;
  const changeSet = param(sp, "change_set_code");
  const entityType = param(sp, "entity_type");
  const entityRef = param(sp, "entity_ref");
  const result = await load(
    apiGetLatest<AuditRow>(
      "/v1/audit-log",
      { change_set_code: changeSet, entity_type: entityType, entity_ref: entityRef },
      { keep: MAX_ROWS },
    ),
  );
  // The API pages oldest → newest; keep the newest MAX_ROWS and show them newest first.
  const rows = result.ok ? [...result.data.items].reverse() : [];

  return (
    <>
      <PageHeader title="Audit Log" subtitle="Append-only record of every change in Merxa: who, through which channel, under which approval." />
      <form className="mx-toolbar" method="get" aria-label="Filter audit log">
        <label className="mx-field">
          <span className="mx-field__label">Change batch</span>
          <input className="mx-input mx-mono" name="change_set_code" defaultValue={changeSet ?? ""} placeholder="CS-0001" />
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Entity type</span>
          <select className="mx-select" name="entity_type" defaultValue={entityType ?? ""}>
            <option value="">All types</option>
            {ENTITY_TYPES.map((t) => (
              <option key={t} value={t}>
                {t.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Entity reference</span>
          <input className="mx-input mx-mono" name="entity_ref" defaultValue={entityRef ?? ""} placeholder="PO-BD-0001#1" />
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {(changeSet || entityType || entityRef) && (
          <Link className="mx-btn mx-btn--ghost" href="/audit">
            Reset
          </Link>
        )}
      </form>
      {result.ok ? (
        <AuditGrid
          rows={rows}
          footer={
            <>
              {result.data.omittedOlder && (
                <strong>
                  Showing the newest {MAX_ROWS.toLocaleString("en-GB")} entries: narrow the filter to see older ones.
                </strong>
              )}
              {!result.data.complete && <strong>The log is very large; the most recent entries may be missing.</strong>}
              <span>Newest first · amber rows came from external systems.</span>
            </>
          }
        />
      ) : (
        <ProblemPanel error={result.error} what="the audit log" />
      )}
    </>
  );
}
