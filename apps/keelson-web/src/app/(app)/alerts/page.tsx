import { EmptyState } from "@keelson/ui";
import { ShieldCheck } from "lucide-react";
import type { Metadata } from "next";
import { AlertsInbox } from "@/components/alerts/AlertsInbox";
import { isAlertId } from "@/lib/alerts/types";
import { canReadCases, canWriteCases } from "@/lib/roles";
import { requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Alerts" };

/**
 * Alerts inbox (M6, `docs/contracts/100` §8). `?alert=<id>` opens that alert's drawer (links from
 * toasts and from a case's "Opened from alert" card). Read roles see alerts; write roles act on them.
 */
export default async function AlertsPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const me = await requireMe();
  if (!canReadCases(me.user)) {
    return (
      <div className="mx-auto flex w-full max-w-chat flex-1 flex-col justify-center px-4 py-12">
        <EmptyState icon={<ShieldCheck />} title="Alerts" headingLevel={1} description="Your role doesn't include alerts." />
      </div>
    );
  }
  const params = await searchParams;
  const requested = typeof params["alert"] === "string" ? params["alert"] : null;
  return <AlertsInbox canWrite={canWriteCases(me.user)} initialAlertId={isAlertId(requested) ? requested : null} />;
}
