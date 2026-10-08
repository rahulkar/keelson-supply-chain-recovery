"use client";

import { IconChevronDown, IconLogout, IconUsers } from "@tabler/icons-react";
import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";

import { apiFetch } from "@/lib/client-api";
import { errorMessage, safeHttpUrl } from "@/lib/http";
import type { SessionUser } from "@/lib/types";

const ROLE_LABELS: Record<string, string> = {
  "merch-planner": "Merchandise Planner",
  "ops-director": "Operations Director",
  "merxa-admin": "Merxa Administrator",
  "integration-writer": "Integration Writer",
};

/** Product roles only (Keycloak adds default-roles-*, offline_access, …). */
export function roleLabels(roles: readonly string[]): string[] {
  return roles.filter((r) => r in ROLE_LABELS).map((r) => ROLE_LABELS[r] as string);
}

export function initials(name: string | null | undefined, fallback: string): string {
  const source = (name || fallback).trim();
  const parts = source.split(/[\s._-]+/).filter(Boolean);
  const letters = parts.length >= 2 ? `${parts[0]?.[0] ?? ""}${parts[parts.length - 1]?.[0] ?? ""}` : source.slice(0, 2);
  return letters.toUpperCase();
}

export function UserMenu({
  user,
  demoMode,
  navigate = (url: string) => window.location.assign(url),
}: {
  user: SessionUser;
  demoMode: boolean;
  navigate?: (url: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const display = user.name || user.username;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function logout() {
    setError(null);
    try {
      const res = await apiFetch<{ status: string; logout_url: string | null }>("/v1/auth/logout", { method: "POST" });
      // Demo deployments return to the persona switcher (persona sessions come from a
      // server-side password grant, so there is no browser SSO session to end); otherwise
      // end the Merxa ID session at the identity provider.
      const idpLogout = safeHttpUrl(res.logout_url);
      navigate(demoMode || !idpLogout ? "/login" : idpLogout);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  const roles = roleLabels(user.roles);
  return (
    <div className="mx-user" ref={ref}>
      <button
        ref={buttonRef}
        type="button"
        className="mx-user__button"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="mx-avatar" aria-hidden="true">
          {initials(user.name, user.username)}
        </span>
        <span>{display}</span>
        <IconChevronDown size={14} aria-hidden="true" />
      </button>
      {open && (
        <div className="mx-user__menu" id={panelId} role="group" aria-label="Account">
          <div className="mx-user__meta">
            <div style={{ fontWeight: 600 }}>{display}</div>
            <div className="mx-muted mx-mono">{user.username}</div>
            {roles.length > 0 && <div className="mx-muted">{roles.join(" · ")}</div>}
          </div>
          {demoMode && (
            <Link href="/login?switch=1" className="mx-menu-item" onClick={() => setOpen(false)}>
              <IconUsers size={16} aria-hidden="true" /> Switch persona
            </Link>
          )}
          <button type="button" className="mx-menu-item" onClick={logout}>
            <IconLogout size={16} aria-hidden="true" /> Sign out
          </button>
          {error && (
            <div className="mx-banner mx-banner--danger" role="alert" style={{ marginTop: 4 }}>
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
