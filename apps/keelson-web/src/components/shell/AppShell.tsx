"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { AlertChannelProvider } from "@/lib/alerts/channel";
import type { Me } from "@/lib/api/types";
import { SessionProvider } from "@/lib/client/session";
import { canReadCases } from "@/lib/roles";
import { HistoryDrawer } from "./HistoryDrawer";
import { PersonaSwitcher } from "./PersonaSwitcher";
import { UserMenu } from "./UserMenu";

function BrandMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="size-6 text-accent">
      <path
        d="M4 13.5c2.6 3.9 13.4 3.9 16 0M12 3v13.8M8.5 7.5h7"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * App chrome: history, brand (home), DEMO persona switcher, user menu. Calm and minimal.
 * People who can read cases also get the live alert channel (Home pill, toasts, inbox updates).
 */
export function AppShell({ me, children }: { me: Me; children: ReactNode }) {
  return (
    <SessionProvider me={me}>
      <AlertsGate me={me}>
        <a href="#main" className="k-skip-link rounded-pill bg-surface px-4 py-2 text-ui font-medium shadow-md">
          Skip to content
        </a>
        <div className="flex h-dvh flex-col">
          <header className="flex h-14 shrink-0 items-center gap-1 px-2 sm:px-4">
            {canReadCases(me.user) ? <HistoryDrawer /> : null}
            <Link
              href="/"
              className="inline-flex items-center gap-2 rounded-pill px-2 py-1 text-body font-semibold tracking-tight text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            >
              <BrandMark />
              {me.brand.engine}
            </Link>
            <div className="ml-auto flex items-center gap-2">
              {me.demo_mode ? <PersonaSwitcher /> : null}
              <UserMenu />
            </div>
          </header>
          <main id="main" tabIndex={-1} className="flex min-h-0 flex-1 flex-col outline-none">
            {children}
          </main>
        </div>
      </AlertsGate>
    </SessionProvider>
  );
}

function AlertsGate({ me, children }: { me: Me; children: ReactNode }) {
  return canReadCases(me.user) ? <AlertChannelProvider>{children}</AlertChannelProvider> : <>{children}</>;
}
