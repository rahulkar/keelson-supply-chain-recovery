"use client";

import { IconAlertTriangle, IconCopy, IconKey, IconPlus } from "@tabler/icons-react";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import { apiFetch } from "@/lib/client-api";
import { formatDateTime, formatRelative } from "@/lib/format";
import { errorMessage } from "@/lib/http";
import { API_KEY_SCOPES, maskedKey } from "@/lib/m2";
import type { ApiKey, MintedApiKey, OAuthClientSeen } from "@/lib/types";
import { useNow } from "@/lib/use-now";
import { idempotencyKeyFor, isRetryable, type PendingAttempt } from "../demo/DemoDirector";
import { Card, EmptyState } from "../ui/Card";
import { StatusBadge } from "../ui/StatusBadge";

/** Default grant of a new key: the plain read scopes. */
export const DEFAULT_KEY_SCOPES: readonly string[] = ["catalog:read", "inventory:read", "po:read", "orders:read"];

/** Known OAuth clients of the merxa realm (infra/keycloak/config/merxa.yaml). */
const KNOWN_CLIENTS: Record<string, string> = {
  "keelson-reader": "Keelson read-only service principal",
  "keelson-preparer": "Keelson change-batch preparer (validate, no apply)",
  "keelson-autocommit": "Keelson auto-policy commits (capped)",
  "keelson-connect": "Keelson delegated approver link",
};

const SURFACES: Record<string, string> = { mcp: "MCP (/mcp)", "mcp-lite": "MCP Lite (/mcp-lite)", rest: "REST API (/v1)" };

/** Own-property lookup: a client id such as "constructor" must not hit Object.prototype. */
function lookup(table: Record<string, string>, key: string | null | undefined): string | undefined {
  return key != null && Object.hasOwn(table, key) ? table[key] : undefined;
}

/** Show-once panel for a freshly minted key (or a notice when the key cannot be shown). */
export function ShowOnceSecret({ label, value, onDone }: { label: string; value: string; onDone: () => void }) {
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied("ok");
    } catch {
      inputRef.current?.select();
      setCopied("failed");
    }
  }
  return (
    <div className="mx-banner mx-banner--warning mx-secret" role="alert">
      <span className="mx-banner__icon" aria-hidden="true">
        <IconAlertTriangle size={16} />
      </span>
      <div className="mx-banner__body">
        <div className="mx-banner__title">Copy the {label} now. It is shown only once and cannot be retrieved later.</div>
        <div className="mx-secret__row">
          <input
            ref={inputRef}
            className="mx-input mx-mono mx-secret__value"
            readOnly
            value={value}
            aria-label={label}
            onFocus={(e) => e.currentTarget.select()}
            spellCheck={false}
            autoComplete="off"
          />
          <button type="button" className="mx-btn mx-btn--sm" onClick={() => void copy()}>
            <IconCopy size={14} aria-hidden="true" /> Copy
          </button>
          <button type="button" className="mx-btn mx-btn--sm mx-btn--primary" onClick={onDone}>
            I have stored it
          </button>
        </div>
        {copied && (
          <div aria-live="polite" className="mx-muted">
            {copied === "ok" ? "Copied to the clipboard." : "Copying is blocked by the browser: the value is selected, press Ctrl+C."}
          </div>
        )}
      </div>
    </div>
  );
}

export function ApiClients({
  initialKeys,
  clients,
  clientsError,
}: {
  initialKeys: ApiKey[];
  clients: OAuthClientSeen[];
  clientsError: string | null;
}) {
  const router = useRouter();
  const now = useNow();
  const ago = (ts: string) => (now === null ? formatDateTime(ts) : formatRelative(ts, now));
  const [keys, setKeys] = useState<ApiKey[]>(initialKeys);
  // `router.refresh()` re-renders the page with fresh server data (e.g. last_used_at, keys
  // revoked elsewhere): the server list then replaces the optimistic local one.
  const [serverKeys, setServerKeys] = useState<ApiKey[]>(initialKeys);
  if (serverKeys !== initialKeys) {
    setServerKeys(initialKeys);
    setKeys(initialKeys);
  }
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([...DEFAULT_KEY_SCOPES]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [minted, setMinted] = useState<MintedApiKey | null>(null);
  const pending = useRef<PendingAttempt | null>(null);

  const toggle = (scope: string) =>
    setScopes((prev) => (prev.includes(scope) ? prev.filter((s) => s !== scope) : [...prev, scope]));

  async function mint(e: FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return setError("Give the key a name (e.g. the system that will use it).");
    if (scopes.length === 0) return setError("Select at least one scope.");
    const body = { name: trimmed, scopes: API_KEY_SCOPES.map((s) => s.scope).filter((s) => scopes.includes(s)) };
    const path = "/v1/api-keys";
    const key = idempotencyKeyFor(pending.current, path, body);
    setBusy("mint");
    setError(null);
    try {
      const created = await apiFetch<MintedApiKey>(path, { method: "POST", body, idempotencyKey: key });
      pending.current = null;
      setMinted(created);
      setName("");
      setKeys((prev) => [
        ...prev.filter((k) => k.id !== created.id),
        {
          id: created.id,
          name: created.name,
          prefix: created.prefix,
          scopes: created.scopes,
          created_by: created.created_by,
          created_at: created.created_at,
          last_used_at: null,
          revoked_at: null,
          version: 1,
          status: "active",
        },
      ]);
      router.refresh();
    } catch (err) {
      pending.current = isRetryable(err) ? { path, body: JSON.stringify(body), key } : null;
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function revoke(k: ApiKey) {
    if (!window.confirm(`Revoke API key "${k.name}" (${maskedKey(k.prefix)})? Systems using it lose access immediately.`)) return;
    setBusy(k.id);
    setError(null);
    try {
      const updated = await apiFetch<Partial<ApiKey>>(`/v1/api-keys/${encodeURIComponent(k.id)}:revoke`, { method: "POST" });
      setKeys((prev) =>
        prev.map((x) => (x.id === k.id ? { ...x, status: "revoked", revoked_at: updated.revoked_at ?? new Date().toISOString() } : x)),
      );
      router.refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const active = keys.filter((k) => k.status === "active").length;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--mx-gap)" }}>
      {minted &&
        (minted.key ? (
          <ShowOnceSecret label={`API key “${minted.name}”`} value={minted.key} onDone={() => setMinted(null)} />
        ) : (
          <div className="mx-banner mx-banner--info" role="status">
            <div className="mx-banner__body">
              Key “{minted.name}” was already created by an earlier attempt, so it cannot be shown again. Revoke it and mint a new
              one if it was not stored.
              <button type="button" className="mx-btn mx-btn--sm" style={{ marginLeft: 8 }} onClick={() => setMinted(null)}>
                Dismiss
              </button>
            </div>
          </div>
        ))}
      {error && (
        <div className="mx-banner mx-banner--danger" role="alert">
          <div className="mx-banner__body">{error}</div>
        </div>
      )}

      <Card title="API keys" meta={`${active} active · ${keys.length - active} revoked`} flush>
        {keys.length === 0 ? (
          <EmptyState title="No API keys">Mint a key below to let another system read Merxa data over REST or MCP Lite.</EmptyState>
        ) : (
          <div className="mx-table-wrap">
            <table className="mx-table" aria-label="API keys">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Key</th>
                  <th scope="col">Scopes</th>
                  <th scope="col">Created</th>
                  <th scope="col">Last used</th>
                  <th scope="col">Status</th>
                  <th scope="col">
                    <span className="mx-visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {[...keys].reverse().map((k) => (
                  <tr key={k.id} data-key-id={k.id}>
                    <td>{k.name}</td>
                    <td className="mx-mono">{maskedKey(k.prefix)}</td>
                    <td>
                      <span className="mx-inline-list mx-scopes">
                        {k.scopes.map((s) => (
                          <code key={s}>{s}</code>
                        ))}
                      </span>
                    </td>
                    <td title={formatDateTime(k.created_at)}>
                      {formatDateTime(k.created_at)} <span className="mx-muted">by {k.created_by}</span>
                    </td>
                    <td title={k.last_used_at ? formatDateTime(k.last_used_at) : undefined}>
                      {k.last_used_at ? ago(k.last_used_at) : <span className="mx-muted">never</span>}
                    </td>
                    <td>
                      <StatusBadge status={k.status} />
                      {k.revoked_at && <span className="mx-muted"> {formatDateTime(k.revoked_at)}</span>}
                    </td>
                    <td className="mx-num">
                      {k.status === "active" && (
                        <button
                          type="button"
                          className="mx-btn mx-btn--sm mx-btn--danger"
                          disabled={busy !== null}
                          onClick={() => void revoke(k)}
                          aria-label={`Revoke ${k.name}`}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Mint an API key">
        <form onSubmit={(e) => void mint(e)} aria-label="Mint an API key" className="mx-form">
          <label className="mx-field" style={{ maxWidth: 360 }}>
            <span className="mx-field__label">Name</span>
            <input
              className="mx-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={100}
              placeholder="e.g. Finance BI export"
              required
            />
          </label>
          <fieldset className="mx-checks">
            <legend className="mx-field__label">Scopes</legend>
            {API_KEY_SCOPES.map((s) => (
              <label key={s.scope} className="mx-check">
                <input type="checkbox" checked={scopes.includes(s.scope)} onChange={() => toggle(s.scope)} />
                <code>{s.scope}</code> <span className="mx-muted">{s.label}</span>
                {s.write && <StatusBadge status="write" tone="warning" label="Write" />}
              </label>
            ))}
          </fieldset>
          <p className="mx-muted" style={{ margin: 0 }}>
            Keys are sent as <code>X-Merxa-Api-Key</code> (or <code>Authorization: Bearer</code>) to <code>/v1/*</code> and{" "}
            <code>/mcp-lite</code>. The scopes are the key&apos;s whole grant; write scopes allow changes without a person approving them.
          </p>
          <div>
            <button type="submit" className="mx-btn mx-btn--primary" disabled={busy !== null}>
              <IconPlus size={14} aria-hidden="true" /> {busy === "mint" ? "Minting…" : "Mint key"}
            </button>
          </div>
        </form>
      </Card>

      <Card title="Connected OAuth clients" meta="Seen on /mcp, /mcp-lite and the REST API" flush>
        {clientsError ? (
          <div className="mx-banner mx-banner--danger" role="alert" style={{ margin: 8 }}>
            <div className="mx-banner__body">Could not load connected clients: {clientsError}</div>
          </div>
        ) : clients.length === 0 ? (
          <EmptyState title="No OAuth clients seen yet">
            Clients appear here after their first call with a Merxa ID access token (e.g. keelson-reader).
          </EmptyState>
        ) : (
          <div className="mx-table-wrap">
            <table className="mx-table" aria-label="Connected OAuth clients">
              <thead>
                <tr>
                  <th scope="col">Client</th>
                  <th scope="col">Description</th>
                  <th scope="col">Last seen</th>
                  <th scope="col">Last surface</th>
                  <th scope="col">Last user / principal</th>
                </tr>
              </thead>
              <tbody>
                {clients.map((c) => (
                  <tr key={c.client_id}>
                    <td className="mx-mono">
                      <IconKey size={13} aria-hidden="true" /> {c.client_id}
                    </td>
                    <td>{lookup(KNOWN_CLIENTS, c.client_id) ?? <span className="mx-muted">—</span>}</td>
                    <td title={formatDateTime(c.last_seen_at)}>
                      {ago(c.last_seen_at)} <span className="mx-muted">({formatDateTime(c.last_seen_at)})</span>
                    </td>
                    <td>{c.surface ? (lookup(SURFACES, c.surface) ?? c.surface) : "—"}</td>
                    <td className="mx-mono">{c.last_username ?? c.last_sub ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
