"use client";

import { cn } from "@keelson/ui";
import Link from "next/link";
import { usePathname } from "next/navigation";

export const AGENTS_TABS = [
  { href: "/admin/agents", label: "Settings" },
  { href: "/admin/agents/specialists", label: "Custom specialists" },
  { href: "/admin/agents/workflow", label: "Workflow" },
] as const;

/** Second-level navigation of the Agents section (links, so each view has its own URL). */
export function AgentsSubnav() {
  const pathname = usePathname() ?? "/admin/agents";
  return (
    <nav aria-label="Agents" className="-mt-2">
      <ul className="flex flex-wrap gap-1 rounded-pill bg-surface-2 p-1">
        {AGENTS_TABS.map((t) => {
          const active = pathname === t.href;
          return (
            <li key={t.href}>
              <Link
                href={t.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "inline-flex h-8 items-center rounded-pill px-3 text-ui font-medium transition-colors duration-150",
                  "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus",
                  active ? "bg-surface text-fg shadow-sm" : "text-fg-muted hover:text-fg",
                )}
              >
                {t.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
