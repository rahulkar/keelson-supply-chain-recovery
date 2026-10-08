/**
 * Alerts API client (M6, `docs/contracts/100` §6/§8) over the app's `ApiClient.call` (same CSRF,
 * `401` and error handling as every other call). Ids are checked before they become URL path
 * segments: alert ids must be canonical UUIDs and pack ids slugs, so a crafted value can never
 * address another route (`../../demo/reset`).
 */
import { ApiClient, ApiError } from "../api/client";
import type { AlertStatus } from "@keelson/contracts";
import {
  alertDetailResponseSchema,
  alertListSchema,
  alertStatusResultSchema,
  demoPackResultSchema,
  demoPacksSchema,
  isAlertId,
  openCaseResultSchema,
  PACK_ID_PATTERN,
  type Alert,
  type AlertDetailView,
  type AlertList,
  type DemoPack,
  type DemoPackAction,
  type DemoPackResult,
  type OpenCaseResult,
} from "./types";

/** Status changes a person makes from the inbox (`POST /api/alerts/{id}/<action>`). */
export type AlertStatusAction = "seen" | "snooze" | "dismiss";

const STATUS_ACTIONS: readonly AlertStatusAction[] = ["seen", "snooze", "dismiss"];
const PACK_ACTIONS: readonly DemoPackAction[] = ["start", "advance", "reset"];

export const ALERT_LIST_LIMIT = 100;
/** The engine's `GET /api/alerts` accepts `limit` 1..200 (a larger value is a 422). */
export const ALERT_LIST_MAX = 200;

export type AlertListQuery = {
  status?: AlertStatus | undefined;
  limit?: number | undefined;
  signal?: AbortSignal | undefined;
};

function invalid(message: string): Promise<never> {
  return Promise.reject(new ApiError(0, "invalid_request", message));
}

export function alertPath(alertId: string, suffix = ""): string {
  if (!isAlertId(alertId)) throw new ApiError(0, "invalid_request", "Invalid alert.");
  return `/api/alerts/${alertId}${suffix}`;
}

export class AlertsApi {
  constructor(private readonly client: ApiClient) {}

  /** `GET /api/alerts?status=&limit=` (tenant from the session; severity is filtered in the inbox). */
  list(query: AlertListQuery = {}): Promise<AlertList> {
    const params = new URLSearchParams();
    if (query.status) params.set("status", query.status);
    const limit = typeof query.limit === "number" && Number.isFinite(query.limit) ? Math.floor(query.limit) : ALERT_LIST_LIMIT;
    params.set("limit", String(Math.min(Math.max(1, limit), ALERT_LIST_MAX)));
    return this.client.call(`/api/alerts?${params.toString()}`, { schema: alertListSchema, signal: query.signal });
  }

  /** `GET /api/alerts/{id}`: alert + event + member signals (attribution) + exposures (+ optional map). */
  get(alertId: string, opts: { signal?: AbortSignal | undefined } = {}): Promise<AlertDetailView> {
    let path: string;
    try {
      path = alertPath(alertId);
    } catch (err) {
      return Promise.reject(err);
    }
    return this.client.call(path, { schema: alertDetailResponseSchema, signal: opts.signal });
  }

  /**
   * `POST /api/alerts/{id}/open-case` + `Idempotency-Key`: the engine creates (at most) one case
   * per alert and queues a `signal` run; a repeat answers the same case.
   */
  openCase(alertId: string, idempotencyKey: string): Promise<OpenCaseResult> {
    let path: string;
    try {
      path = alertPath(alertId, "/open-case");
    } catch (err) {
      return Promise.reject(err);
    }
    return this.client.call(path, { method: "POST", body: {}, idempotencyKey, schema: openCaseResultSchema });
  }

  /** `POST /api/alerts/{id}/seen|snooze|dismiss` → the updated alert when the engine returns it. */
  setStatus(alertId: string, action: AlertStatusAction): Promise<Alert | null> {
    if (!STATUS_ACTIONS.includes(action)) return invalid("Invalid alert action.");
    let path: string;
    try {
      path = alertPath(alertId, `/${action}`);
    } catch (err) {
      return Promise.reject(err);
    }
    return this.client.call(path, { method: "POST", body: {}, schema: alertStatusResultSchema });
  }

  /** DEMO presenter: replayable scenario packs (`GET /api/demo/packs`). */
  demoPacks(): Promise<DemoPack[]> {
    return this.client.call("/api/demo/packs", { schema: demoPacksSchema });
  }

  /** DEMO presenter: `POST /api/demo/packs/{pack}/start|advance|reset` (engine proxy to the hub). */
  packAction(packId: string, action: DemoPackAction): Promise<DemoPackResult> {
    if (!PACK_ID_PATTERN.test(packId)) return invalid("Invalid scenario pack.");
    if (!PACK_ACTIONS.includes(action)) return invalid("Invalid scenario action.");
    return this.client.call(`/api/demo/packs/${packId}/${action}`, {
      method: "POST",
      body: {},
      schema: demoPackResultSchema,
    });
  }
}
