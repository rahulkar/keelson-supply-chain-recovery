import Link from "next/link";

import { changeSetCodeFromCreatedVia } from "@/lib/attribution";
import { ExternalChip } from "./StatusBadge";

/** Renders `created_via` of a record; change-set origins link to the batch with an amber chip. */
export function CreatedVia({ value }: { value: string | null | undefined }) {
  const code = changeSetCodeFromCreatedVia(value);
  if (!code) return <span>{value || "—"}</span>;
  return (
    <span className="mx-inline-list">
      <ExternalChip title={`Created by change batch ${code}`} />
      <Link href={`/change-batches/${encodeURIComponent(code)}`} className="mx-mono">
        {code}
      </Link>
    </span>
  );
}
