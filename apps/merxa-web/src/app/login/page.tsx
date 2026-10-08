import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { PersonaSwitcher } from "@/components/shell/PersonaSwitcher";
import { ApiError, safeNext } from "@/lib/http";
import { apiGet, findMe } from "@/lib/server-api";
import type { Persona } from "@/lib/types";

export const metadata: Metadata = { title: "Sign in" };

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

function first(v: string | string[] | undefined): string | undefined {
  return Array.isArray(v) ? v[0] : v;
}

async function loadPersonas(): Promise<{ personas: Persona[]; unavailable: string | null }> {
  try {
    return { personas: await apiGet<Persona[]>("/v1/auth/demo/personas", undefined, { allowAnonymous: true }), unavailable: null };
  } catch (err) {
    if (err instanceof ApiError) {
      // 404 = demo mode off (regular sign-in only); anything else is shown to the user.
      return { personas: [], unavailable: err.status === 404 ? null : err.problem.detail || err.message };
    }
    throw err;
  }
}

export default async function LoginPage({ searchParams }: Props) {
  const sp = await searchParams;
  const next = safeNext(first(sp.next));
  const switching = first(sp.switch) === "1";
  let me = null;
  let meError: string | null = null;
  try {
    me = await findMe();
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    meError = err.problem.detail || err.message;
  }
  if (me && !switching) redirect(next);
  const { personas, unavailable } = await loadPersonas();
  const oidcHref = `/v1/auth/login?next=${encodeURIComponent(next)}`;

  return (
    <div className="mx-login">
      <aside className="mx-login__aside">
        <div className="mx-brand" style={{ background: "transparent", border: 0, padding: 0 }}>
          <span className="mx-brand__mark" aria-hidden="true">
            M
          </span>
          <span>
            Merxa <span className="mx-brand__suite">Retail Suite</span>
          </span>
        </div>
        <p className="mx-login__claim">
          Merchandise, supply and stock operations in one ledger.
          <small>
            Purchase orders, inventory positions, transfer orders and audited change batches for European retail
            networks.
          </small>
        </p>
        <span className="mx-muted" style={{ color: "var(--mx-chrome-text-muted)" }}>
          © Merxa Systems
        </span>
      </aside>
      <main className="mx-login__panel">
        <div>
          <h1 style={{ fontSize: "var(--mx-text-xl)" }}>Sign in to Merxa</h1>
          <p className="mx-muted" style={{ margin: "4px 0 0" }}>
            {me ? (
              <>
                Signed in as <strong>{me.user.name || me.user.username}</strong>. Pick another persona to switch.
              </>
            ) : (
              "Use your Merxa ID, or pick a demo persona."
            )}
          </p>
        </div>
        {meError && (
          <div className="mx-banner mx-banner--danger" role="alert">
            Merxa services are unavailable: {meError}
          </div>
        )}
        <a className="mx-btn mx-btn--primary" href={oidcHref} style={{ height: 34 }}>
          Sign in with Merxa ID
        </a>
        {(personas.length > 0 || unavailable) && <div className="mx-divider">Demo personas</div>}
        {unavailable && (
          <div className="mx-banner mx-banner--warning" role="alert">
            Persona switcher unavailable: {unavailable}
          </div>
        )}
        {personas.length > 0 && (
          <PersonaSwitcher personas={personas} next={next} currentUsername={me?.user.username ?? null} />
        )}
      </main>
    </div>
  );
}
