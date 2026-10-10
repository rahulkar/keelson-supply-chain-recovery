import { EmptyState } from "@keelson/ui";
import { ShieldAlert } from "lucide-react";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { EvalCaseDetail } from "@/components/admin/evals/EvalCaseDetail";
import { isUuid } from "@/lib/admin/evalRuns";
import { hasAnyRole } from "@/lib/roles";
import { requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Eval case · Admin" };

const CASE_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

/** One case of an eval run (docs/contracts/128 §6): platform admins only. */
export default async function AdminEvalCaseRoute({ params }: { params: Promise<{ runId: string; caseId: string }> }) {
  const { runId, caseId } = await params;
  const id = decodeURIComponent(caseId);
  if (!isUuid(runId) || !CASE_ID.test(id)) notFound();
  const me = await requireMe();
  if (!hasAnyRole(me.user, ["platform-admin"])) {
    return <EmptyState icon={<ShieldAlert />} headingLevel={1} title="Platform admins only" description="Eval results cover the whole platform." />;
  }
  return <EvalCaseDetail runId={runId} caseId={id} />;
}
