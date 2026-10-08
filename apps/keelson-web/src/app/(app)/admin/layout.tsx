import { EmptyState } from "@keelson/ui";
import { ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";
import { AdminShell } from "@/components/admin/AdminShell";
import { isAdmin } from "@/lib/roles";
import { requireMe } from "@/lib/server/session";

/** Back-office (`70-backoffice-m2.md` §6): tenant-admin / platform-admin only. */
export default async function AdminLayout({ children }: { children: ReactNode }) {
  const me = await requireMe();
  if (!isAdmin(me.user)) {
    return (
      <div className="mx-auto w-full max-w-chat px-4 py-12">
        <EmptyState
          icon={<ShieldAlert />}
          title="Admins only"
          headingLevel={1}
          description="Admin needs the tenant admin or platform admin role."
        />
      </div>
    );
  }
  return <AdminShell>{children}</AdminShell>;
}
