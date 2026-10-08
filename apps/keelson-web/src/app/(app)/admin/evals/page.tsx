import { EmptyState } from "@keelson/ui";
import { ShieldAlert } from "lucide-react";
import type { Metadata } from "next";
import { EvalsPage } from "@/components/admin/EvalsPage";
import { hasAnyRole } from "@/lib/roles";
import { requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Evals · Admin" };

/** Eval results are platform-wide (not tenant data): platform admins only, like the engine route. */
export default async function AdminEvalsRoute() {
  const me = await requireMe();
  if (!hasAnyRole(me.user, ["platform-admin"])) {
    return <EmptyState icon={<ShieldAlert />} headingLevel={1} title="Platform admins only" description="Eval results cover the whole platform." />;
  }
  return <EvalsPage />;
}
