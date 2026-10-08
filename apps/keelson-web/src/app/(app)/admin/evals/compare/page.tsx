import { EmptyState } from "@keelson/ui";
import { ShieldAlert } from "lucide-react";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { EvalCompareView } from "@/components/admin/evals/EvalCompare";
import { isUuid } from "@/lib/admin/evalRuns";
import { hasAnyRole } from "@/lib/roles";
import { requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Compare eval runs · Admin" };

/** Two eval runs side by side (`?base=&head=`, docs/contracts/128 §6): platform admins only. */
export default async function AdminEvalCompareRoute({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const q = await searchParams;
  const base = typeof q["base"] === "string" ? q["base"] : "";
  const head = typeof q["head"] === "string" ? q["head"] : "";
  if (!isUuid(base) || !isUuid(head)) notFound();
  const me = await requireMe();
  if (!hasAnyRole(me.user, ["platform-admin"])) {
    return <EmptyState icon={<ShieldAlert />} headingLevel={1} title="Platform admins only" description="Eval results cover the whole platform." />;
  }
  return <EvalCompareView base={base} head={head} />;
}
