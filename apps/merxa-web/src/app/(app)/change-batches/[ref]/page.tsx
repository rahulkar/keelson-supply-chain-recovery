import type { Metadata } from "next";
import Link from "next/link";

import { AttributionBanner } from "@/components/change-batches/AttributionBanner";
import { AuditTable } from "@/components/change-batches/AuditTable";
import { ItemDiffTable } from "@/components/change-batches/ItemDiffTable";
import { Card, KeyValues } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { coApproverNames, joinNames } from "@/lib/attribution";
import { formatCents, formatDateTime, formatInt } from "@/lib/format";
import { appPath, decodeParam } from "@/lib/http";
import { apiGet, apiGetAll, load } from "@/lib/server-api";
import type { AuditRow, ChangeSet, OperatorGuide } from "@/lib/types";

type Props = { params: Promise<{ ref: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { ref } = await params;
  return { title: `Change batch ${decodeParam(ref)}` };
}

function Mono({ children }: { children: string | null | undefined }) {
  return children ? <span className="mx-mono">{children}</span> : <>—</>;
}

export default async function ChangeBatchPage({ params }: Props) {
  const { ref } = await params;
  const cs = await apiGet<ChangeSet>(`/v1/change-sets/${encodeURIComponent(decodeParam(ref))}`, undefined, {
    notFoundOn404: true,
  });
  const [audit, guide] = await Promise.all([
    load(apiGetAll<AuditRow>("/v1/audit-log", { change_set_code: cs.code }, { max: 3000 })),
    load(apiGet<OperatorGuide>(`/v1/change-sets/${encodeURIComponent(cs.change_set_id)}/operator-guide`)),
  ]);
  const totals = cs.preview?.totals;
  const conflicts = cs.result?.conflicts ?? [];
  const origin = cs.origin ?? {};

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Change Batches", href: "/change-batches" }, { label: cs.code }]}
        title={
          <>
            <span className="mx-mono">{cs.code}</span> {cs.title}
          </>
        }
        badge={<StatusBadge status={cs.status} />}
        subtitle={`${formatInt(cs.items_count)} item${cs.items_count === 1 ? "" : "s"} · created ${formatDateTime(cs.created_at)}`}
      />

      <AttributionBanner cs={cs} />

      {cs.status === "conflict" && conflicts.length > 0 && (
        <div className="mx-banner mx-banner--warning" role="alert">
          <div className="mx-banner__body">
            <div className="mx-banner__title">Apply was rejected: {conflicts.length} item(s) no longer match current data</div>
            Nothing was changed. The submitting system must re-validate the batch against current Merxa records.
          </div>
        </div>
      )}

      <Card title="Batch details">
        <KeyValues
          items={[
            { label: "Status", value: <StatusBadge status={cs.status} /> },
            { label: "Origin system", value: origin.system || "—" },
            { label: "Case reference", value: <Mono>{origin.case_ref ? String(origin.case_ref) : null}</Mono> },
            { label: "Plan version", value: origin.plan_version ?? "—" },
            { label: "Created by client", value: <Mono>{cs.created_by_client}</Mono> },
            { label: "Created", value: formatDateTime(cs.created_at) },
            { label: "Validated", value: formatDateTime(cs.validated_at) },
            { label: "Applied", value: formatDateTime(cs.applied_at) },
            { label: "Approval reference", value: <Mono>{cs.approval_ref}</Mono> },
            { label: "Approved by", value: <Mono>{cs.approved_by}</Mono> },
            ...(coApproverNames(cs.co_approvers).length
              ? [{ label: "Co-approved by", value: joinNames(coApproverNames(cs.co_approvers)) }]
              : []),
            { label: "Initiated by", value: cs.initiated_by || "—" },
            {
              label: "Applied by",
              value: cs.applied_by.display ? (
                <>
                  {cs.applied_by.display} <span className="mx-muted mx-mono">({cs.applied_by.username})</span>
                </>
              ) : (
                "—"
              ),
            },
            { label: "Lines (preview)", value: totals ? formatInt(totals.lines) : "—" },
            { label: "Units moved", value: totals ? formatInt(totals.units_moved) : "—" },
            { label: "Value at cost", value: totals ? formatCents(totals.value_cents) : "—" },
            { label: "Preview hash", value: cs.preview_hash ? <span className="mx-hash">{cs.preview_hash}</span> : "—" },
          ]}
        />
      </Card>

      <Card title="Items: before and after" meta={`${formatInt(cs.items.length)} item(s) · changed values highlighted`} flush>
        <ItemDiffTable items={cs.items} conflicts={conflicts} />
      </Card>

      <Card
        title="Audit trail"
        meta={audit.ok ? `${formatInt(audit.data.items.length)} entr${audit.data.items.length === 1 ? "y" : "ies"}` : undefined}
        actions={
          <Link className="mx-btn mx-btn--sm" href={`/audit?change_set_code=${encodeURIComponent(cs.code)}`}>
            Open in Audit Log
          </Link>
        }
        flush
      >
        {audit.ok ? <AuditTable rows={audit.data.items} /> : <ProblemPanel error={audit.error} what="audit entries" />}
      </Card>

      {guide.ok && guide.data.steps.length > 0 && (
        <Card title="Operator guide" meta="Manual equivalent of each item, with deep links" flush>
          <ol className="mx-steps">
            {guide.data.steps.map((s) => {
              const href = appPath(s.deep_link);
              return (
                <li key={s.seq}>
                  <div>
                    <strong>{s.title}</strong>
                    <div className="mx-muted">{s.instructions}</div>
                    {href && (
                      <Link href={href} aria-label={`Open in Merxa: ${s.title}`}>
                        Open in Merxa
                      </Link>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        </Card>
      )}
    </>
  );
}
