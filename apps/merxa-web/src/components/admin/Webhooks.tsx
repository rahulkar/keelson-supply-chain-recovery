"use client";

import { IconPlayerPause, IconPlayerPlay, IconPlus, IconRefresh, IconTrash } from "@tabler/icons-react";
import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { useRouter } from "next/navigation";
import { useMemo, useRef, useState, useTransition, type FormEvent } from "react";

import { apiFetch } from "@/lib/client-api";
import { formatDateTime, formatInt } from "@/lib/format";
import { ApiError, errorMessage } from "@/lib/http";
import { WEBHOOK_EVENT_TYPES, deliveryState, displayWebhookUrl, webhookUrlProblem } from "@/lib/m2";
import type { CreatedWebhookSubscription, WebhookDelivery, WebhookSubscription } from "@/lib/types";
import { dateTimeCol, monoCol, numberCol } from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";
import { idempotencyKeyFor, isRetryable, type PendingAttempt } from "../demo/DemoDirector";
import { Card, EmptyState } from "../ui/Card";
import { StatusBadge } from "../ui/StatusBadge";
import { ShowOnceSecret } from "./ApiClients";

type DeliveryRow = WebhookDelivery & { subscription_name: string; state: string };

/** 412: changed since the page loaded; 404: deleted meanwhile. */
function isStale(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 412 || err.status === 404);
}

function staleMessage(err: unknown, name: string): string | null {
  if (!(err instanceof ApiError)) return null;
  if (err.status === 412) return `Subscription “${name}” was changed by someone else. The list has been reloaded: check it and try again.`;
  if (err.status === 404) return `Subscription “${name}” no longer exists. The list has been reloaded.`;
  return null;
}

function DeliveryStateCell(p: ICellRendererParams<DeliveryRow>) {
  if (!p.data) return null;
  return <StatusBadge status={p.data.state} />;
}

/** Webhook subscriptions (create, pause/resume, delete) and recent deliveries with their status. */
export function Webhooks({
  subscriptions: serverSubscriptions,
  deliveries,
  deliveriesError,
}: {
  subscriptions: WebhookSubscription[];
  deliveries: WebhookDelivery[];
  deliveriesError: string | null;
}) {
  const router = useRouter();
  const [refreshing, startRefresh] = useTransition();
  // Results of this page's own writes (new version / active flag, deletions) until the
  // reloaded server list arrives, so the next If-Match carries the version just written.
  const [local, setLocal] = useState<Record<string, Pick<WebhookSubscription, "active" | "version"> | "deleted">>({});
  const [seenServer, setSeenServer] = useState(serverSubscriptions);
  if (seenServer !== serverSubscriptions) {
    setSeenServer(serverSubscriptions);
    setLocal({});
  }
  const subscriptions = useMemo(
    () =>
      serverSubscriptions.flatMap((s) => {
        const o = Object.hasOwn(local, s.id) ? local[s.id] : undefined;
        if (o === "deleted") return [];
        return o ? [{ ...s, ...o }] : [s];
      }),
    [serverSubscriptions, local],
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedWebhookSubscription | null>(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>([...WEBHOOK_EVENT_TYPES]);
  const [showForm, setShowForm] = useState(false);
  const pending = useRef<PendingAttempt | null>(null);

  const names = useMemo(() => Object.fromEntries(subscriptions.map((s) => [s.id, s.name])), [subscriptions]);
  const rows = useMemo<DeliveryRow[]>(
    () => deliveries.map((d) => ({ ...d, subscription_name: names[d.subscription_id] ?? d.subscription_id, state: deliveryState(d) })),
    [deliveries, names],
  );
  const columns = useMemo<ColDef<DeliveryRow>[]>(
    () => [
      dateTimeCol("created_at", "Queued", { pinned: "left" }),
      monoCol("event_type", "Event", { width: 200 }),
      numberCol("event_seq", "Event #", { width: 90 }),
      { field: "subscription_name", headerName: "Subscription", width: 190 },
      { field: "state", headerName: "Status", width: 110, cellRenderer: DeliveryStateCell },
      numberCol("attempt", "Attempts", { width: 100 }),
      numberCol("response_code", "HTTP", { width: 80, valueFormatter: (p) => (p.value == null ? "—" : String(p.value)) }),
      { field: "last_error", headerName: "Last error", flex: 1, minWidth: 200, valueFormatter: (p) => (p.value as string | null) ?? "" },
      dateTimeCol("next_attempt_at", "Next attempt"),
      dateTimeCol("delivered_at", "Delivered"),
      monoCol("event_id", "Webhook id", { width: 280 }),
    ],
    [],
  );
  const rowClassRules = useMemo(() => ({ "mx-row-risk": (p: { data?: DeliveryRow }) => p.data?.state === "failed" }), []);

  const refresh = () => startRefresh(() => router.refresh());
  // Row actions are also held while the reload after a write is in flight.
  const locked = busy !== null || refreshing;

  async function create(e: FormEvent) {
    e.preventDefault();
    const urlProblem = webhookUrlProblem(url);
    if (!name.trim()) return setError("Give the subscription a name.");
    if (urlProblem) return setError(urlProblem);
    if (events.length === 0) return setError("Select at least one event type.");
    const body = { name: name.trim(), url: url.trim(), events: WEBHOOK_EVENT_TYPES.filter((t) => events.includes(t)) };
    const path = "/v1/webhook-subscriptions";
    const key = idempotencyKeyFor(pending.current, path, body);
    setBusy("create");
    setError(null);
    try {
      const sub = await apiFetch<CreatedWebhookSubscription>(path, { method: "POST", body, idempotencyKey: key });
      pending.current = null;
      setCreated(sub);
      setName("");
      setUrl("");
      setShowForm(false);
      refresh();
    } catch (err) {
      pending.current = isRetryable(err) ? { path, body: JSON.stringify(body), key } : null;
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  async function setActive(sub: WebhookSubscription, active: boolean) {
    setBusy(sub.id);
    setError(null);
    try {
      const updated = await apiFetch<Partial<WebhookSubscription>>(`/v1/webhook-subscriptions/${encodeURIComponent(sub.id)}`, {
        method: "PATCH",
        body: { active },
        ifMatch: sub.version,
      });
      // Without this a quick second click (Pause, then Resume) resent the version the page
      // was rendered with and failed with a misleading "changed by someone else" (412).
      if (typeof updated?.version === "number") {
        setLocal((prev) => ({ ...prev, [sub.id]: { active: updated.active ?? active, version: updated.version as number } }));
      }
      refresh();
    } catch (err) {
      setError(staleMessage(err, sub.name) ?? errorMessage(err));
      // Changed or deleted elsewhere: reload so the next attempt uses the current version.
      if (isStale(err)) refresh();
    } finally {
      setBusy(null);
    }
  }

  async function remove(sub: WebhookSubscription) {
    if (!window.confirm(`Delete the webhook subscription "${sub.name}"? Its delivery history is deleted too.`)) return;
    setBusy(sub.id);
    setError(null);
    try {
      await apiFetch(`/v1/webhook-subscriptions/${encodeURIComponent(sub.id)}`, { method: "DELETE" });
      setLocal((prev) => ({ ...prev, [sub.id]: "deleted" }));
      refresh();
    } catch (err) {
      setError(staleMessage(err, sub.name) ?? errorMessage(err));
      if (isStale(err)) refresh();
    } finally {
      setBusy(null);
    }
  }

  const toggleEvent = (t: string) => setEvents((prev) => (prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]));
  const counts = rows.reduce<Record<string, number>>((acc, r) => ({ ...acc, [r.state]: (acc[r.state] ?? 0) + 1 }), {});

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--mx-gap)" }}>
      {created &&
        (created.secret ? (
          <ShowOnceSecret label={`signing secret of “${created.name}”`} value={created.secret} onDone={() => setCreated(null)} />
        ) : (
          <div className="mx-banner mx-banner--info" role="status">
            <div className="mx-banner__body">
              Subscription “{created.name}” was already created by an earlier attempt; its signing secret cannot be shown again.
              <button type="button" className="mx-btn mx-btn--sm" style={{ marginLeft: 8 }} onClick={() => setCreated(null)}>
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

      <Card
        title="Subscriptions"
        meta={`${subscriptions.filter((s) => s.active).length} active`}
        actions={
          <button type="button" className="mx-btn mx-btn--sm" onClick={() => setShowForm((v) => !v)} aria-expanded={showForm}>
            <IconPlus size={14} aria-hidden="true" /> New subscription
          </button>
        }
        flush
      >
        {showForm && (
          <form onSubmit={(e) => void create(e)} aria-label="New webhook subscription" className="mx-form mx-form--inset">
            <div className="mx-toolbar">
              <label className="mx-field">
                <span className="mx-field__label">Name</span>
                <input className="mx-input" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} required />
              </label>
              <label className="mx-field" style={{ flex: 1, minWidth: 280 }}>
                <span className="mx-field__label">Endpoint URL</span>
                <input
                  className="mx-input mx-mono"
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  maxLength={2048}
                  placeholder="https://example.org/webhooks/merxa"
                  required
                />
              </label>
            </div>
            <fieldset className="mx-checks">
              <legend className="mx-field__label">Events</legend>
              {WEBHOOK_EVENT_TYPES.map((t) => (
                <label key={t} className="mx-check">
                  <input type="checkbox" checked={events.includes(t)} onChange={() => toggleEvent(t)} />
                  <code>{t}</code>
                </label>
              ))}
            </fieldset>
            <p className="mx-muted" style={{ margin: 0 }}>
              Deliveries follow Standard Webhooks: <code>webhook-id</code>, <code>webhook-timestamp</code> and an HMAC-SHA256{" "}
              <code>webhook-signature</code>; failed deliveries are retried after 1 s, 5 s, 30 s, 2 min and 10 min.
            </p>
            <div style={{ display: "flex", gap: 8 }}>
              <button type="submit" className="mx-btn mx-btn--primary" disabled={busy !== null}>
                {busy === "create" ? "Creating…" : "Create subscription"}
              </button>
              <button type="button" className="mx-btn mx-btn--ghost" onClick={() => setShowForm(false)}>
                Cancel
              </button>
            </div>
          </form>
        )}
        {subscriptions.length === 0 ? (
          <EmptyState title="No webhook subscriptions">Create one to push Merxa events to another system.</EmptyState>
        ) : (
          <div className="mx-table-wrap">
            <table className="mx-table" aria-label="Webhook subscriptions">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Endpoint</th>
                  <th scope="col">Events</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="mx-num">
                    Delivered
                  </th>
                  <th scope="col" className="mx-num">
                    Pending
                  </th>
                  <th scope="col" className="mx-num">
                    Failed
                  </th>
                  <th scope="col">Last delivery</th>
                  <th scope="col">
                    <span className="mx-visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {subscriptions.map((s) => (
                  <tr key={s.id} data-subscription-id={s.id}>
                    <td>
                      {s.name}
                      <div className="mx-muted">
                        {s.created_by ? `by ${s.created_by}, ` : ""}
                        {formatDateTime(s.created_at)}
                      </div>
                    </td>
                    {/* Admin-entered text: rendered as text, never as a link; a password in the URL is masked. */}
                    <td className="mx-mono mx-break" style={{ minWidth: 220 }}>{displayWebhookUrl(s.url)}</td>
                    <td>
                      <span className="mx-inline-list mx-scopes">
                        {s.events.map((e) => (
                          <code key={e}>{e}</code>
                        ))}
                      </span>
                    </td>
                    <td>
                      <StatusBadge status={s.active ? "active" : "paused"} />
                    </td>
                    <td className="mx-num">{formatInt(s.delivered)}</td>
                    <td className="mx-num">{formatInt(s.pending)}</td>
                    <td className={s.failed > 0 ? "mx-num mx-text-danger" : "mx-num"}>{formatInt(s.failed)}</td>
                    <td>{formatDateTime(s.last_delivered_at)}</td>
                    <td className="mx-num" style={{ whiteSpace: "nowrap" }}>
                      <button
                        type="button"
                        className="mx-btn mx-btn--sm"
                        disabled={locked}
                        onClick={() => void setActive(s, !s.active)}
                        aria-label={`${s.active ? "Pause" : "Resume"} ${s.name}`}
                      >
                        {s.active ? <IconPlayerPause size={14} aria-hidden="true" /> : <IconPlayerPlay size={14} aria-hidden="true" />}
                        {s.active ? "Pause" : "Resume"}
                      </button>{" "}
                      <button
                        type="button"
                        className="mx-btn mx-btn--sm mx-btn--danger"
                        disabled={locked}
                        onClick={() => void remove(s)}
                        aria-label={`Delete ${s.name}`}
                      >
                        <IconTrash size={14} aria-hidden="true" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <section aria-label="Recent deliveries" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <h2 className="mx-section-title" style={{ margin: 0 }}>
            Recent deliveries
          </h2>
          <span className="mx-muted">
            {(["delivered", "pending", "retrying", "failed"] as const)
              .filter((k) => counts[k])
              .map((k) => `${counts[k]} ${k}`)
              .join(" · ") || "none"}
          </span>
          <span className="mx-toolbar__spacer" style={{ flex: 1 }} />
          <button type="button" className="mx-btn mx-btn--sm" onClick={refresh} disabled={refreshing}>
            <IconRefresh size={14} aria-hidden="true" /> {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        {deliveriesError ? (
          <div className="mx-banner mx-banner--danger" role="alert">
            <div className="mx-banner__body">Could not load deliveries: {deliveriesError}</div>
          </div>
        ) : (
          <DataGrid<DeliveryRow>
            label="Webhook deliveries"
            rows={rows}
            columns={columns}
            getRowId={(r) => r.id}
            rowClassRules={rowClassRules}
            exportFileName="merxa-webhook-deliveries"
            emptyMessage="No deliveries yet."
            footer={<span>Newest first. A delivery is retried up to 6 attempts, then marked failed. The webhook id stays the same on every retry.</span>}
          />
        )}
      </section>
    </div>
  );
}
