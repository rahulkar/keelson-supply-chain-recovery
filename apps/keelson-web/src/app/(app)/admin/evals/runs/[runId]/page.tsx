import { EmptyState } from "@keelson/ui";
import { ShieldAlert } from "lucide-react";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { EvalRunDetail } from "@/components/admin/evals/EvalRunDetail";
import { isUuid } from "@/lib/admin/evalRuns";
import { hasAnyRole } from "@/lib/roles";
import { requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Eval run · Admin" };

/** One eval run (docs/contracts/128 §6): platform admins only, like the engine route. */
export default async function AdminEvalRunRoute({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  if (!isUuid(runId)) notFound();
  const me = await requireMe();
  if (!hasAnyRole(me.user, ["platform-admin"])) {
    return <EmptyState icon={<ShieldAlert />} headingLevel={1} title="Platform admins only" description="Eval results cover the whole platform." />;
  }
  return <EvalRunDetail runId={runId} />;
}
