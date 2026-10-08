"use client";

/**
 * The tenant's live alert channel (M6, `docs/contracts/100` §6/§8), mounted once in the app shell:
 *
 * - the "N new alerts" count: `GET /api/alerts?status=new` on mount (and after a reconnect), then
 *   kept current from the stream (an alert is counted while its status is `new`);
 * - `GET /api/alerts/events` (SSE, AG-UI `CUSTOM` events named `sc.alert.*`, engine
 *   `core/alerts.py`): `sc.alert.raised {alert, renotify}` = a new alert or a re-notification
 *   (severity band rose), `sc.alert.updated {alert}`, `sc.alert.removed {alert_id}`,
 *   `sc.alert.count {new}`, `sc.alert.case_opened {alert_id, case_id}`; the alert itself as the
 *   value, or `{alert, new_count}`, is accepted too;
 * - a toast for a newly raised **high / critical** alert (once per alert and severity), also when
 *   the engine already opened its draft case (`alert_and_draft_case` → status `opened`);
 * - subscribers (the inbox) get every update; the inbox registers itself as the opener so a
 *   toast's "View alert" opens the drawer in place when the inbox is on screen.
 *
 * Fatal stream drops check the session (a `401` goes to sign-in) and reconnect with backoff.
 * Alert titles may come from news headlines: they are only ever rendered as text.
 */
import { parseAgUiEvent } from "@keelson/contracts";
import { toast } from "@keelson/ui";
import { SEVERITY_META } from "@keelson/viz";
import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useEffectEvent, useMemo, useRef, useState, type ReactNode } from "react";
import { useSession } from "../client/session";
import type { EventSourceLike } from "../stream/useCaseStream";
import { AlertsApi } from "./client";
import { alertSchema, isAlertId, type Alert } from "./types";

export const ALERT_EVENTS_URL = "/api/alerts/events";
export const ALERT_EVENT_PREFIX = "sc.alert.";
/** Updates this page made itself (never sent by the engine). */
export const LOCAL_UPDATE = "sc.alert.local";
export const ALERT_RAISED = "sc.alert.raised";
export const ALERT_REMOVED = "sc.alert.removed";
export const ALERT_COUNT = "sc.alert.count";
export const ALERT_CASE_OPENED = "sc.alert.case_opened";
/** Events that may notify (new alert, or severity rose by ≥ the rule's hysteresis). */
export const NOTIFY_EVENT_NAMES: readonly string[] = [ALERT_RAISED];
/** Statuses that still deserve a toast: unseen, or a case the engine drafted on its own. */
const TOAST_STATUSES: readonly Alert["status"][] = ["new", "opened"];
/**
 * Only real alerts touched this recently toast (a replayed backlog never floods the screen).
 * Simulated (DEMO pack) alerts carry the pack's SimClock time, days in the past, so they are
 * exempt: they only reach the stream while a presenter replays the pack.
 */
export const TOAST_MAX_AGE_MS = 15 * 60_000;

export type AlertUpdate = {
  name: string;
  alert: Alert | null;
  /** The alert this event is about (`alert.id`, or `alert_id` of removed / case_opened). */
  alertId: string | null;
  newCount: number | null;
  /** `sc.alert.removed`: the alert is gone (e.g. a demo pack reset). */
  removed?: boolean | undefined;
  /** `sc.alert.case_opened`: the case now linked to `alertId`. */
  caseId?: string | null | undefined;
};

export type AlertChannelValue = {
  /** New (unseen) alerts; `null` while unknown (not loaded yet, or the API is unavailable). */
  newCount: number | null;
  api: AlertsApi;
  /** Every alert update from the stream or from this page's own actions. */
  subscribe: (listener: (u: AlertUpdate) => void) => () => void;
  /** This page changed an alert (dismiss, snooze, seen, opened): keep the count in step. */
  applyLocal: (alert: Alert) => void;
  /** The inbox opens alerts in place (toast "View alert"); returns the unregister function. */
  registerOpener: (open: (alertId: string) => void) => () => void;
};

const AlertChannelContext = createContext<AlertChannelValue | null>(null);

/** Parses one SSE payload into an alert update, or `null` when it is not an `sc.alert.*` event. */
export function parseAlertEvent(data: string): AlertUpdate | null {
  const parsed = parseAgUiEvent(data);
  if (!parsed.ok || parsed.event.type !== "CUSTOM") return null;
  const ev = parsed.event as { name?: unknown; value?: unknown };
  if (typeof ev.name !== "string" || !ev.name.startsWith(ALERT_EVENT_PREFIX)) return null;
  const value = ev.value && typeof ev.value === "object" && !Array.isArray(ev.value) ? (ev.value as Record<string, unknown>) : {};
  const direct = alertSchema.safeParse(value);
  const nested = direct.success ? direct : alertSchema.safeParse(value["alert"]);
  const alert = nested.success ? nested.data : null;
  const count = (n: unknown) => (typeof n === "number" && Number.isInteger(n) && n >= 0 ? n : null);
  const refId = value["alert_id"];
  const update: AlertUpdate = {
    name: ev.name,
    alert,
    alertId: alert?.id ?? (isAlertId(refId) ? refId : null),
    newCount: count(value["new_count"]) ?? (ev.name === ALERT_COUNT ? count(value["new"]) : null),
  };
  if (ev.name === ALERT_REMOVED) update.removed = update.alertId !== null;
  if (ev.name === ALERT_CASE_OPENED) {
    const c = value["case_id"];
    update.caseId = typeof c === "string" && c.length > 0 && c.length <= 200 ? c : null;
  }
  return update;
}

/** Whether an update deserves a toast (pure; `notified` holds `id:severity` keys already shown). */
export function shouldToast(update: AlertUpdate, notified: ReadonlySet<string>, now: number = Date.now()): boolean {
  const a = update.alert;
  if (!a || !NOTIFY_EVENT_NAMES.includes(update.name)) return false;
  if (!TOAST_STATUSES.includes(a.status) || (a.severity !== "high" && a.severity !== "critical")) return false;
  if (!a.simulated) {
    const touched = Date.parse(a.updated_at);
    if (Number.isNaN(touched) || now - touched > TOAST_MAX_AGE_MS) return false;
  }
  return !notified.has(`${a.id}:${a.severity}`);
}

const NOTIFIED_KEY = "keelson.alerts.notified";

function loadNotified(): Set<string> {
  try {
    const raw = window.sessionStorage.getItem(NOTIFIED_KEY);
    const list: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(list) ? list.filter((x): x is string => typeof x === "string").slice(-200) : []);
  } catch {
    return new Set();
  }
}

function saveNotified(keys: ReadonlySet<string>): void {
  try {
    window.sessionStorage.setItem(NOTIFIED_KEY, JSON.stringify([...keys].slice(-200)));
  } catch {
    // Private mode / blocked storage: toasts are then de-duplicated for this page only.
  }
}

function defaultEventSource(url: string): EventSourceLike | null {
  if (typeof EventSource === "undefined") return null;
  return new EventSource(url, { withCredentials: true });
}

const CLOSED = 2;
const MAX_BACKOFF_MS = 30_000;

export function AlertChannelProvider({
  children,
  createEventSource = defaultEventSource,
  onOpenAlert,
}: {
  children: ReactNode;
  /** Test seam (jsdom has no EventSource). `null` = no live channel. */
  createEventSource?: ((url: string) => EventSourceLike | null) | undefined;
  /** Toast action; default: open the alert in the inbox. */
  onOpenAlert?: ((alertId: string) => void) | undefined;
}) {
  const { api: client } = useSession();
  const router = useRouter();
  const api = useMemo(() => new AlertsApi(client), [client]);
  const [newIds, setNewIds] = useState<ReadonlySet<string> | null>(null);
  const [serverCount, setServerCount] = useState<number | null>(null);
  const listeners = useRef(new Set<(u: AlertUpdate) => void>());
  const openers = useRef<((alertId: string) => void)[]>([]);
  const notified = useRef<Set<string> | null>(null);
  const factory = useRef(createEventSource);
  const openRef = useRef(onOpenAlert);
  const routerRef = useRef(router);
  useEffect(() => {
    factory.current = createEventSource;
    openRef.current = onOpenAlert;
    routerRef.current = router;
  });

  /** An alert is new (`true`) or not any more (`false`: seen, opened, removed …). */
  const apply = useCallback((alertId: string, isNew: boolean) => {
    setServerCount(null);
    setNewIds((prev) => {
      const next = new Set(prev ?? []);
      if (isNew) next.add(alertId);
      else next.delete(alertId);
      return next;
    });
  }, []);

  const listNew = useEffectEvent(() => api.list({ status: "new" }));

  const openAlert = useCallback((id: string) => {
    const inPlace = openers.current.at(-1);
    if (inPlace) inPlace(id);
    else if (openRef.current) openRef.current(id);
    else routerRef.current.push(`/alerts?alert=${encodeURIComponent(id)}`);
  }, []);

  useEffect(() => {
    let disposed = false;
    let source: EventSourceLike | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let backoff = 1000;
    const resync = () => {
      listNew().then(
        (list) => {
          if (disposed) return;
          setNewIds(new Set(list.items.filter((a) => a.status === "new").map((a) => a.id)));
          setServerCount(list.newCount);
        },
        () => undefined, // count stays unknown; the pill then just says "Alerts"
      );
    };
    resync();

    const handle = (update: AlertUpdate) => {
      if (update.alert) apply(update.alert.id, update.alert.status === "new");
      else if (update.alertId && (update.removed || update.caseId)) apply(update.alertId, false);
      if (update.newCount !== null) setServerCount(update.newCount);
      notified.current ??= loadNotified();
      if (update.removed && update.alertId) {
        // Pack ids are deterministic (uuid5): a reset + replay raises the same alert again and
        // must toast again, so a removed alert forgets its toasts.
        const prefix = `${update.alertId}:`;
        const before = notified.current.size;
        for (const k of [...notified.current]) if (k.startsWith(prefix)) notified.current.delete(k);
        if (notified.current.size !== before) saveNotified(notified.current);
      }
      if (shouldToast(update, notified.current) && update.alert) {
        const a = update.alert;
        notified.current.add(`${a.id}:${a.severity}`);
        saveNotified(notified.current);
        toast(`${SEVERITY_META[a.severity].label} alert: ${a.title}`, {
          type: a.severity === "critical" ? "error" : "info",
          description: a.why,
          timeout: 12_000,
          action: { label: "View alert", onClick: () => openAlert(a.id) },
        });
      }
      for (const l of listeners.current) l(update);
    };

    const connect = () => {
      if (disposed) return;
      const es = factory.current(ALERT_EVENTS_URL);
      if (!es) return;
      source = es;
      es.onopen = () => {
        if (disposed || source !== es) return;
        backoff = 1000;
      };
      es.onmessage = (msg: MessageEvent) => {
        if (disposed || source !== es) return;
        const update = parseAlertEvent(typeof msg.data === "string" ? msg.data : "");
        if (update) handle(update);
      };
      es.onerror = () => {
        if (disposed || source !== es || es.readyState !== CLOSED) return;
        es.close();
        source = null;
        const delay = backoff;
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        void (async () => {
          try {
            await client.me(); // a 401 sends the person to sign-in
          } catch (err) {
            if (err && typeof err === "object" && (err as { status?: number }).status === 401) return;
          }
          if (disposed) return;
          timer = setTimeout(() => {
            connect();
            resync(); // events missed while offline
          }, delay);
        })();
      };
    };
    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      source?.close();
      source = null;
    };
  }, [apply, client, openAlert]);

  const subscribe = useCallback((listener: (u: AlertUpdate) => void) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const applyLocal = useCallback(
    (alert: Alert) => {
      apply(alert.id, alert.status === "new");
      for (const l of listeners.current) l({ name: LOCAL_UPDATE, alert, alertId: alert.id, newCount: null });
    },
    [apply],
  );

  const registerOpener = useCallback((open: (alertId: string) => void) => {
    openers.current.push(open);
    return () => {
      openers.current = openers.current.filter((o) => o !== open);
    };
  }, []);

  const newCount = serverCount ?? (newIds ? newIds.size : null);
  const value = useMemo(
    () => ({ newCount, api, subscribe, applyLocal, registerOpener }),
    [newCount, api, subscribe, applyLocal, registerOpener],
  );
  return <AlertChannelContext.Provider value={value}>{children}</AlertChannelContext.Provider>;
}

/** The alert channel, or `null` outside the provider (roles that cannot read cases). */
export function useAlertChannel(): AlertChannelValue | null {
  return useContext(AlertChannelContext);
}
