"use client";

import { cn } from "@keelson/ui";
import { Activity, BookOpen, Bot, Building2, ClipboardCheck, Cpu, FlaskConical, LayoutDashboard, Plug, Radar, ScrollText, ShieldCheck, Users } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";

type NavItem = { href: string; label: string; icon: ReactNode; platformOnly?: boolean };

export const ADMIN_NAV: NavItem[] = [
  { href: "/admin", label: "Overview", icon: <LayoutDashboard /> },
  { href: "/admin/changes", label: "Pending changes", icon: <ClipboardCheck /> },
  { href: "/admin/gateway", label: "AI Gateway", icon: <Cpu /> },
  { href: "/admin/agents", label: "Agents", icon: <Bot /> },
  { href: "/admin/policies", label: "Policies", icon: <ShieldCheck /> },
  { href: "/admin/connectors", label: "Connectors", icon: <Plug /> },
  { href: "/admin/knowledge", label: "Knowledge", icon: <BookOpen /> },
  { href: "/admin/signals", label: "Signals", icon: <Radar /> },
  { href: "/admin/users", label: "Users", icon: <Users /> },
  { href: "/admin/tenants", label: "Tenants", icon: <Building2 />, platformOnly: true },
  { href: "/admin/audit", label: "Audit", icon: <ScrollText /> },
  { href: "/admin/evals", label: "Evals", icon: <FlaskConical />, platformOnly: true },
];

/** Requests waiting for a second admin (nav badge); hidden while unknown (older engine, error). */
function usePendingCount(pathname: string): number | null {
  const admin = useAdminApi();
  // Re-read when the admin moves between pages (a decision elsewhere changes the count).
  const pending = useResource(() => admin.pendingChangeCount(), `pending-count:${pathname}`);
  return pending.data ?? null;
}

export function isActive(pathname: string, href: string): boolean {
  return href === "/admin" ? pathname === "/admin" : pathname === href || pathname.startsWith(`${href}/`);
}

/** Back-office frame: left nav (top strip on small screens), compact density, same tokens. */
export function AdminShell({ children }: { children: ReactNode }) {
  const pathname = usePathname() ?? "/admin";
  const { me } = useSession();
  const platform = hasAnyRole(me.user, ["platform-admin"]);
  const items = ADMIN_NAV.filter((i) => !i.platformOnly || platform);
  const pendingCount = usePendingCount(pathname);
  return (
    <div className="flex min-h-0 flex-1 flex-col md:flex-row" data-density="compact">
      <nav aria-label="Admin" className="shrink-0 border-b border-border px-2 md:w-52 md:border-r md:border-b-0 md:px-3 md:py-4">
        <p className="hidden px-2 pb-2 text-caption font-medium tracking-wide text-fg-subtle uppercase md:block">Admin</p>
        <ul className="flex gap-1 overflow-x-auto py-2 md:flex-col md:overflow-visible md:py-0">
          {items.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <li key={item.href} className="shrink-0">
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex h-8 items-center gap-2 rounded-sm px-2 text-ui whitespace-nowrap transition-colors duration-150",
                    "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus [&>svg]:size-4",
                    active ? "bg-accent-soft font-medium text-accent-soft-fg" : "text-fg-muted hover:bg-surface-2 hover:text-fg",
                  )}
                >
                  <span aria-hidden="true" className="inline-flex [&>svg]:size-4">
                    {item.icon}
                  </span>
                  {item.label}
                  {item.href === "/admin/changes" && pendingCount ? (
                    <span className="ml-auto rounded-pill bg-warning-soft px-1.5 text-caption font-medium text-warning tabular-nums">
                      <span aria-hidden="true">{pendingCount}</span>
                      <span className="sr-only">, {pendingCount} waiting</span>
                    </span>
                  ) : null}
                </Link>
              </li>
            );
          })}
        </ul>
        <p className="hidden items-center gap-1.5 px-2 pt-4 text-caption text-fg-subtle md:flex">
          <Activity aria-hidden="true" className="size-3.5" />
          {me.user.active_tenant ?? "No workspace"}
        </p>
      </nav>
      <div className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 px-4 py-5 md:px-6">{children}</div>
      </div>
    </div>
  );
}
