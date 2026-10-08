"use client";

import type { AlertSeverity, AlertStatus } from "@keelson/contracts";
import { ALERT_SEVERITIES, ALERT_STATUSES } from "@keelson/contracts";
import { Button, EmptyState, Field, Select, Skeleton } from "@keelson/ui";
import { DemoBadge, KindIcon, SEVERITY_META, SeverityBadge, kindLabel } from "@keelson/viz";
import { BellOff } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { describeError } from "@/lib/api/client";
import { useAlertChannel, type AlertUpdate } from "@/lib/alerts/channel";
import { STATUS_LABELS, exposureSummaryText, relativeTime } from "@/lib/alerts/format";
import { isAlertId, type Alert } from "@/lib/alerts/types";
import { AlertDrawer } from "./AlertDrawer";

/** "open" = what still needs attention (new, seen, case opened); "all" = every status. */
export type StatusFilter = "open" | "all" | AlertStatus;
export type SeverityFilter = "all" | AlertSeverity;

const OPEN_STATUSES: readonly AlertStatus[] = ["new", "seen", "opened"];

export function matchesFilters(alert: Alert, status: StatusFilter, severity: SeverityFilter): boolean {
  if (status === "open" && !OPEN_STATUSES.includes(alert.status)) return false;
  if (status !== "open" && status !== "all" && alert.status !== status) return false;
  return severity === "all" || alert.severity === severity;
}

/** The inbox rows after one live update (pure). */
export function applyUpdate(items: Alert[], u: AlertUpdate): Alert[] {
  if (u.alert) {
    const a = u.alert;
    return [a, ...items.filter((x) => x.id !== a.id)];
  }
  if (u.alertId && u.removed) return items.filter((x) => x.id !== u.alertId);
  if (u.alertId && u.caseId) {
    return items.map((x) =>
      x.id === u.alertId && x.case_id === null ? { ...x, case_id: u.caseId!, status: x.status === "resolved" ? x.status : "opened" } : x,
    );
  }
  return items;
}

/** Newest activity first. */
export function sortAlerts(alerts: readonly Alert[]): Alert[] {
  return [...alerts].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
}

const STATUS_OPTIONS = [
  { value: "open", label: "Needs attention" },
  { value: "all", label: "All" },
  ...ALERT_STATUSES.map((s) => ({ value: s, label: STATUS_LABELS[s] })),
];

const SEVERITY_OPTIONS = [
  { value: "all", label: "Any severity" },
  ...[...ALERT_SEVERITIES].reverse().map((s) => ({ value: s, label: SEVERITY_META[s].label })),
];

type Load = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; items: Alert[] };
/** A finished load for one query key (stale answers never show under a newer key). */
type Settled = { key: string } & ({ status: "error"; message: string } | { status: "ready"; items: Alert[] });

function AlertRow({ alert, onOpen, now }: { alert: Alert; onOpen: (id: string) => void; now: number }) {
  const titleId = `alert-title-${alert.id}`;
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(alert.id)}
        aria-labelledby={titleId}
        aria-describedby={`alert-meta-${alert.id}`}
        data-alert-id={alert.id}
        data-status={alert.status}
        className="flex w-full items-start gap-3 px-3 py-3 text-left hover:bg-surface-2 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus sm:px-4"
      >
        <KindIcon kind={alert.kind ?? ""} />
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex items-start gap-2">
            {alert.status === "new" ? <span aria-hidden="true" className="mt-1.5 size-2 shrink-0 rounded-pill bg-accent" /> : null}
            <span id={titleId} className={`min-w-0 flex-1 text-ui break-words text-fg ${alert.status === "new" ? "font-semibold" : "font-medium"}`}>
              {alert.title}
            </span>
            <time dateTime={alert.updated_at} className="shrink-0 text-caption text-fg-subtle">
              {relativeTime(alert.updated_at, now)}
            </time>
          </span>
          <span className="line-clamp-2 text-caption text-fg-muted">{alert.why}</span>
          <span id={`alert-meta-${alert.id}`} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-fg-muted">
            <SeverityBadge severity={alert.severity} size="sm" />
            {alert.kind ? <span className="sr-only">{kindLabel(alert.kind)}</span> : null}
            <span>{exposureSummaryText(alert.exposure_summary)}</span>
            {alert.status !== "new" ? <span>· {STATUS_LABELS[alert.status]}</span> : <span className="sr-only">· New</span>}
            {alert.simulated ? <DemoBadge /> : null}
          </span>
        </span>
      </button>
    </li>
  );
}

/**
 * Alerts inbox (`/alerts`, M6 `docs/contracts/100` §8): a calm list (severity chip, kind icon,
 * title, why, exposure summary, time, DEMO badge) with status / severity filters; the detail lives
 * in the drawer. Live: the tenant alert channel updates rows in place.
 */
export function AlertsInbox({ canWrite, initialAlertId = null }: { canWrite: boolean; initialAlertId?: string | null | undefined }) {
  const channel = useAlertChannel();
  const api = channel?.api;
  const [status, setStatus] = useState<StatusFilter>("open");
  const [severity, setSeverity] = useState<SeverityFilter>("all");
  const [settled, setSettled] = useState<Settled | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<string | null>(isAlertId(initialAlertId) ? initialAlertId : null);
  const [now, setNow] = useState(() => Date.now());

  // Specific statuses are asked of the server; "open" / "all" read every status and filter here.
  const serverStatus = status === "open" || status === "all" ? undefined : status;
  const key = `${serverStatus ?? "*"}#${attempt}`;
  useEffect(() => {
    if (!api) return;
    const ctrl = new AbortController();
    api
      .list({ status: serverStatus, signal: ctrl.signal })
      .then((list) => {
        if (ctrl.signal.aborted) return;
        setNow(Date.now());
        setSettled({ key, status: "ready", items: list.items });
      })
      .catch((err: unknown) => {
        if (!ctrl.signal.aborted) setSettled({ key, status: "error", message: describeError(err) });
      });
    return () => ctrl.abort();
  }, [api, serverStatus, key]);
  const load = useMemo<Load>(() => (!settled || settled.key !== key ? { status: "loading" } : settled), [settled, key]);

  // Live updates: replace a known alert, add a new one, drop a removed one, link an opened case.
  useEffect(() => {
    if (!channel) return;
    return channel.subscribe((u) => {
      setSettled((prev) => {
        if (!prev || prev.status !== "ready") return prev;
        const items = applyUpdate(prev.items, u);
        return items === prev.items ? prev : { ...prev, items };
      });
      if (u.alert) setNow(Date.now());
    });
  }, [channel]);

  // Relative times stay fresh while the page is open.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(t);
  }, []);

  const select = useCallback((id: string | null) => {
    setSelected(id);
    try {
      window.history.replaceState(window.history.state, "", id ? `/alerts?alert=${encodeURIComponent(id)}` : "/alerts");
    } catch {
      // Not in a browser history context (tests); the drawer state is enough.
    }
  }, []);

  // While the inbox is on screen a toast's "View alert" opens the drawer here (a router push to
  // the same page would keep this component mounted and its drawer closed).
  useEffect(() => {
    if (!channel) return;
    return channel.registerOpener((id) => {
      if (isAlertId(id)) select(id);
    });
  }, [channel, select]);

  const visible = useMemo(
    () => (load.status === "ready" ? sortAlerts(load.items.filter((a) => matchesFilters(a, status, severity))) : []),
    [load, status, severity],
  );
  const filtered = status !== "open" || severity !== "all";

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4 py-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-heading font-semibold text-fg">Alerts</h1>
        <p className="text-ui text-fg-muted">Hazards, news and supplier messages that touch your sites, lanes and orders.</p>
      </div>

      <div role="group" aria-label="Filter alerts" className="flex flex-wrap items-end gap-3">
        <Field label="Status" className="w-48">
          <Select value={status} onChange={(v) => setStatus(v as StatusFilter)} options={STATUS_OPTIONS} />
        </Field>
        <Field label="Severity" className="w-40">
          <Select value={severity} onChange={(v) => setSeverity(v as SeverityFilter)} options={SEVERITY_OPTIONS} />
        </Field>
        <p className="ml-auto text-caption text-fg-muted" aria-live="polite">
          {load.status === "ready" ? (visible.length === 1 ? "1 alert" : `${visible.length} alerts`) : ""}
        </p>
      </div>

      {load.status === "loading" ? (
        <div role="status" aria-label="Loading alerts" className="flex flex-col gap-2">
          <Skeleton className="h-20 w-full rounded-lg" />
          <Skeleton className="h-20 w-full rounded-lg" />
          <Skeleton className="h-20 w-full rounded-lg" />
        </div>
      ) : load.status === "error" ? (
        <div className="flex flex-col items-start gap-3 rounded-lg border border-border p-4">
          <p className="text-ui text-fg">Couldn&apos;t load alerts. {load.message}</p>
          <Button size="sm" onClick={() => setAttempt((n) => n + 1)}>
            Try again
          </Button>
        </div>
      ) : visible.length === 0 ? (
        <EmptyState
          icon={<BellOff />}
          title={filtered ? "No alerts match these filters" : "No alerts"}
          description={
            filtered
              ? "Try another status or severity."
              : "When a hazard, a news report or a supplier message affects your network, it appears here with its likely impact."
          }
          action={
            filtered ? undefined : (
              <Link href="/" className="text-ui font-medium text-accent underline-offset-4 hover:underline">
                Describe a disruption instead
              </Link>
            )
          }
        />
      ) : (
        <ul aria-label="Alerts" className="flex flex-col divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface">
          {visible.map((a) => (
            <AlertRow key={a.id} alert={a} onOpen={select} now={now} />
          ))}
        </ul>
      )}

      <AlertDrawer alertId={selected} onClose={() => select(null)} canWrite={canWrite} />
    </div>
  );
}

