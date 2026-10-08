import { EmptyState } from "@keelson/ui";
import { FolderOpen, ShieldCheck } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { CaseList } from "@/components/cases/CaseList";
import { loginUrl } from "@/lib/paths";
import { canReadCases } from "@/lib/roles";
import { listCases } from "@/lib/server/engine";
import { EngineUnavailableError, requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Cases" };

export default async function CasesPage() {
  const me = await requireMe();
  if (!canReadCases(me.user)) {
    return (
      <div className="mx-auto w-full max-w-chat px-4 py-12">
        <EmptyState icon={<ShieldCheck />} headingLevel={1} title="No access to cases" description="Your role doesn't include working on cases." />
      </div>
    );
  }
  const res = await listCases(100);
  if (res.kind === "unauthenticated") redirect(loginUrl("/cases"));
  if (res.kind === "unavailable") throw new EngineUnavailableError(res.detail);
  const items = res.kind === "ok" ? res.data : [];

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-chat flex-col gap-6 px-4 py-6">
        <h1 className="text-heading font-semibold text-fg">Cases</h1>
        {items.length === 0 ? (
          <EmptyState
            icon={<FolderOpen />}
            title="No cases yet"
            description="Describe a disruption on the home screen to start one."
            action={
              <Link href="/" className="text-ui font-medium text-accent underline-offset-4 hover:underline">
                Start a case
              </Link>
            }
          />
        ) : (
          <CaseList items={items} />
        )}
      </div>
    </div>
  );
}
