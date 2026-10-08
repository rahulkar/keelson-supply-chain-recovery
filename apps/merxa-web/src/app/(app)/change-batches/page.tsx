import type { Metadata } from "next";
import Link from "next/link";

import { ChangeBatchGrid } from "@/components/change-batches/ChangeBatchGrid";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { buildQuery } from "@/lib/http";
import { apiGet, load } from "@/lib/server-api";
import type { ChangeSetSummary, Page } from "@/lib/types";
import { param, type SearchParams } from "@/lib/params";

export const metadata: Metadata = { title: "Change Batches" };

const STATUSES = ["draft", "validated", "applying", "applied", "conflict", "failed", "rolled_back"] as const;
const PAGE_SIZE = 200;

type Props = { searchParams: Promise<SearchParams> };


export default async function ChangeBatchesPage({ searchParams }: Props) {
  const sp = await searchParams;
  const status = STATUSES.find((s) => s === param(sp, "status"));
  const cursor = param(sp, "cursor");
  const result = await load(apiGet<Page<ChangeSetSummary>>("/v1/change-sets", { status, cursor, limit: PAGE_SIZE }));

  return (
    <>
      <PageHeader
        title="Change Batches"
        subtitle="Bulk changes submitted through the Merxa API and integrations, with before/after preview, approval and audit."
      />
      <form className="mx-toolbar" method="get" aria-label="Filter change batches">
        <label className="mx-field">
          <span className="mx-field__label">Status</span>
          <select className="mx-select" name="status" defaultValue={status ?? ""}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {(status || cursor) && (
          <Link className="mx-btn mx-btn--ghost" href="/change-batches">
            Reset
          </Link>
        )}
      </form>
      {result.ok ? (
        <ChangeBatchGrid
          rows={result.data.items}
          footer={
            <>
              {cursor && <Link href={`/change-batches${buildQuery({ status })}`}>First page</Link>}
              {result.data.next_cursor && (
                <Link href={`/change-batches${buildQuery({ status, cursor: result.data.next_cursor })}`}>Older batches →</Link>
              )}
              <span>Newest first · amber rows were applied in the last 10 minutes</span>
            </>
          }
        />
      ) : (
        <ProblemPanel error={result.error} what="change batches" />
      )}
    </>
  );
}
