import Link from "next/link";
import type { ReactNode } from "react";

import { DensityToggle } from "@/components/shell/DensityProvider";
import { ExternalChangesBanner } from "@/components/shell/ExternalChangesBanner";
import { Sidebar } from "@/components/shell/Sidebar";
import { UserMenu } from "@/components/shell/UserMenu";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { apiGet, getMe, load } from "@/lib/server-api";
import type { ChangeSetSummary, Page } from "@/lib/types";

/** Authenticated application shell: spruce chrome (brand, top bar, module nav). */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const meResult = await load(getMe()); // redirects to /login without a valid session
  if (!meResult.ok) {
    return (
      <main className="mx-content" style={{ maxWidth: 720, margin: "48px auto" }}>
        <ProblemPanel error={meResult.error} what="your Merxa session" />
        <Link className="mx-btn" href="/login">
          Go to sign-in
        </Link>
      </main>
    );
  }
  const me = meResult.data;
  // Best-effort: the notice's client poll recovers from a failed initial load.
  const recent = await load(apiGet<Page<ChangeSetSummary>>("/v1/change-sets", { status: "applied", limit: 25 }));
  const applied: ChangeSetSummary[] = recent.ok ? recent.data.items : [];
  const isAdmin = me.user.roles.includes("merxa-admin");
  return (
    <div className="mx-shell">
      <a className="mx-skip-link" href="#main">
        Skip to main content
      </a>
      <div className="mx-brand">
        <span className="mx-brand__mark" aria-hidden="true">
          M
        </span>
        <span className="mx-brand__text">
          {me.brand.vendor || "Merxa"} <span className="mx-brand__suite">Retail Suite</span>
        </span>
      </div>
      <div className="mx-topbar">
        <span className="mx-topbar__title">Operations</span>
        {me.demo_mode && (
          <span className="mx-topbar__env" title="Demo environment: data is synthetic">
            Demo
          </span>
        )}
        <span className="mx-topbar__spacer" />
        <DensityToggle />
        <UserMenu user={me.user} demoMode={me.demo_mode} />
      </div>
      <Sidebar showDemoDirector={me.demo_mode && isAdmin} showAdmin={isAdmin} />
      <main className="mx-main" id="main" tabIndex={-1}>
        <ExternalChangesBanner initial={applied} />
        <div className="mx-content">{children}</div>
      </main>
    </div>
  );
}
