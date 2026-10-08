"use client";

import { IconArrowRight, IconLoader2 } from "@tabler/icons-react";
import { useState } from "react";

import { apiFetch } from "@/lib/client-api";
import { errorMessage } from "@/lib/http";
import type { Persona } from "@/lib/types";

/**
 * Demo persona switcher (DEMO_MODE only): `POST /v1/auth/demo/login-as {username}` creates a
 * Merxa session server side (password grant on `merxa-demo`) and sets the HttpOnly
 * `merxa_session` cookie. No credentials ever reach the browser.
 */
export function PersonaSwitcher({
  personas,
  next,
  currentUsername,
  navigate = (url: string) => window.location.assign(url),
}: {
  personas: Persona[];
  next: string;
  currentUsername: string | null;
  navigate?: (url: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function loginAs(username: string) {
    setBusy(username);
    setError(null);
    try {
      await apiFetch("/v1/auth/demo/login-as", { method: "POST", body: { username } });
      // Full navigation so every server component re-renders with the new session.
      navigate(next);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(null);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
        {personas.map((p) => (
          <li key={p.username}>
            <button
              type="button"
              className="mx-persona"
              onClick={() => loginAs(p.username)}
              disabled={busy !== null}
              aria-current={currentUsername === p.username ? "true" : undefined}
              aria-label={`Continue as ${p.name}, ${p.role_label}`}
            >
              <span className="mx-avatar mx-avatar--lg" aria-hidden="true">
                {p.initials}
              </span>
              <span>
                <span className="mx-persona__name">{p.name}</span>
                <br />
                <span className="mx-persona__role">
                  {p.role_label} · <span className="mx-mono">{p.username}</span>
                </span>
              </span>
              <span className="mx-persona__go" aria-hidden="true">
                {busy === p.username ? <IconLoader2 size={16} /> : <IconArrowRight size={16} />}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {error && (
        <div className="mx-banner mx-banner--danger" role="alert">
          Could not sign in: {error}
        </div>
      )}
    </div>
  );
}
