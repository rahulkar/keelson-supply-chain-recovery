import { EmptyState } from "@keelson/ui";
import { ShieldAlert } from "lucide-react";
import type { Metadata } from "next";
import { TenantsPage } from "@/components/admin/TenantsPage";
import { hasAnyRole } from "@/lib/roles";
import { requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Tenants · Admin" };

export default async function AdminTenantsRoute() {
  const me = await requireMe();
  if (!hasAnyRole(me.user, ["platform-admin"])) {
    return (
      <EmptyState icon={<ShieldAlert />} headingLevel={1} title="Platform admins only" description="Tenants are managed by platform admins." />
    );
  }
  return <TenantsPage />;
}
