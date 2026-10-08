import { EmptyState } from "@keelson/ui";
import { ShieldCheck } from "lucide-react";
import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { CaseView } from "@/components/case/CaseView";
import { loginUrl } from "@/lib/paths";
import { canReadCases } from "@/lib/roles";
import { getCase } from "@/lib/server/engine";
import { EngineUnavailableError, requireMe } from "@/lib/server/session";

type Params = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { id } = await params;
  const res = await getCase(id);
  return { title: res.kind === "ok" ? res.data.case.title || "Case" : "Case" };
}

export default async function CasePage({ params }: Params) {
  const { id } = await params;
  const me = await requireMe();
  if (!canReadCases(me.user)) {
    return (
      <div className="mx-auto w-full max-w-chat px-4 py-12">
        <EmptyState icon={<ShieldCheck />} headingLevel={1} title="No access to cases" description="Your role doesn't include working on cases." />
      </div>
    );
  }
  const res = await getCase(id);
  switch (res.kind) {
    case "ok":
      // Keyed by case id: navigating between cases starts a fresh stream and state.
      return <CaseView key={res.data.case.id} initial={res.data} />;
    case "not_found":
      return notFound();
    case "unauthenticated":
      return redirect(loginUrl(`/cases/${encodeURIComponent(id)}`));
    case "forbidden":
      return (
        <div className="mx-auto w-full max-w-chat px-4 py-12">
          <EmptyState icon={<ShieldCheck />} headingLevel={1} title="No access to this case" description="Ask a planner on the case to share it." />
        </div>
      );
    case "unavailable":
      throw new EngineUnavailableError(res.detail);
  }
}
