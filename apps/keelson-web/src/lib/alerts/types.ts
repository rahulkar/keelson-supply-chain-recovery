/**
 * Response shapes of the engine's Alerts API (`docs/contracts/100` §6/§8) as the web reads them.
 *
 * Alerts and alert details are the contract models (`signalsAlertSchema`,
 * `signalsAlertDetailSchema`, generated from contracts-py). Unknown top-level fields are dropped
 * (`strip`) so an additive engine field never breaks the inbox; one malformed alert in a list is
 * skipped instead of failing the whole list.
 */
import {
  networkMapDataSchema,
  networkMapPropsSchema,
  signalsAlertDetailSchema,
  signalsAlertSchema,
  signalsEventMembershipSchema,
  signalsSignalSchema,
  type SignalsAlert,
  type SignalsAlertDetail,
  type SignalsEventMembership,
  type SignalsSignal,
} from "@keelson/contracts";
import { z } from "zod";

/** An alert as the web reads it: the contract model + the event `kind` (optional engine extra for the kind icon). */
export type Alert = SignalsAlert & { kind?: SignalsSignal["kind"] | undefined };
export type AlertDetail = SignalsAlertDetail;
export type AlertSignal = AlertDetail["signals"][number];
export type AlertExposure = AlertDetail["exposures"][number];
export type AlertEvent = AlertDetail["event"];
/** Why a member signal belongs to the event (M8 clustering, `docs/contracts/123` §5). */
export type AlertMembership = SignalsEventMembership;

/** One alert, tolerant of additive fields; keeps an optional `kind` (event kind) when the engine sends it. */
export const alertSchema = signalsAlertSchema.extend({ kind: signalsSignalSchema.shape.kind.optional().catch(undefined) }).strip();

/** Canonical lowercase UUID (every Signal Hub id is a uuid5). */
export const ALERT_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function isAlertId(value: unknown): value is string {
  return typeof value === "string" && ALERT_ID_PATTERN.test(value);
}

/** Valid alerts of a list (`{items}` or a bare array); malformed entries are skipped. */
function validAlerts(items: unknown[]): Alert[] {
  const out: Alert[] = [];
  for (const item of items) {
    const parsed = alertSchema.safeParse(item);
    if (parsed.success) out.push(parsed.data);
  }
  return out;
}

export const alertListSchema = z
  .union([z.array(z.unknown()), z.object({ items: z.array(z.unknown()), new_count: z.number().int().nonnegative().optional() }).loose()])
  .transform((v) => {
    const items = Array.isArray(v) ? v : v.items;
    const newCount = Array.isArray(v) ? undefined : v.new_count;
    const valid = validAlerts(items);
    return { items: valid, newCount: newCount ?? null, skipped: items.length - valid.length };
  });
export type AlertList = z.infer<typeof alertListSchema>;

/**
 * Optional engine extra on `GET /api/alerts/{id}`: a ready `network_map@1` payload (the engine
 * knows the tenant network's coordinates; the hub's exposures only name node codes). When absent
 * or invalid the drawer draws the event footprint alone.
 */
export const alertMapSchema = z.object({ props: networkMapPropsSchema, data: networkMapDataSchema }).strip();
export type AlertMap = z.infer<typeof alertMapSchema>;

const detailBase = z.object({
  alert: alertSchema,
  event: signalsAlertDetailSchema.shape.event.strip(),
  signals: z.array(z.unknown()),
  exposures: z.array(z.unknown()),
  memberships: z.array(z.unknown()).optional(),
  map: z.unknown().optional(),
}).loose();

const signalItemSchema = signalsAlertDetailSchema.shape.signals.element.strip();
const exposureItemSchema = signalsAlertDetailSchema.shape.exposures.element.strip();
const membershipItemSchema = signalsEventMembershipSchema.strip();

/**
 * `GET /api/alerts/{id}`: the contract `AlertDetail` (+ optional `map`); bad sub-items are skipped.
 * `memberships` (M8, optional: an older hub sends none) keeps only valid entries of this event whose
 * signal is in `signals`.
 */
export const alertDetailResponseSchema = detailBase.transform((v) => {
  const signals = v.signals.flatMap((s) => {
    const p = signalItemSchema.safeParse(s);
    return p.success ? [p.data] : [];
  });
  const exposures = v.exposures.flatMap((e) => {
    const p = exposureItemSchema.safeParse(e);
    return p.success ? [p.data] : [];
  });
  const map = alertMapSchema.safeParse(v.map);
  const signalIds = new Set(signals.map((s) => s.id));
  const memberships = (v.memberships ?? []).flatMap((m) => {
    const p = membershipItemSchema.safeParse(m);
    return p.success && p.data.event_id === v.event.id && signalIds.has(p.data.signal_id) ? [p.data] : [];
  });
  return {
    alert: v.alert,
    event: v.event,
    signals,
    exposures,
    memberships,
    map: map.success ? map.data : null,
  } satisfies AlertDetail & { map: AlertMap | null };
});
export type AlertDetailView = z.infer<typeof alertDetailResponseSchema>;

/** `POST /api/alerts/{id}/open-case` → the (one) case of this alert and the queued signal run. */
export const openCaseResultSchema = z
  .object({ case_id: z.string().min(1).max(200), run_id: z.string().nullable().optional(), created: z.boolean().optional() })
  .loose();
export type OpenCaseResult = z.infer<typeof openCaseResultSchema>;

/** `POST /api/alerts/{id}/dismiss|snooze|seen` → the updated alert (or nothing). */
export const alertStatusResultSchema = z
  .union([z.object({ alert: alertSchema }).loose().transform((v) => v.alert), alertSchema, z.object({}).loose().transform(() => null)])
  .catch(null);

export const PACK_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,62}$/;

const count = z.number().int().nonnegative().nullable().optional().catch(undefined);

/**
 * Demo scenario packs the presenter can replay (`GET /api/demo/packs`). Accepts the web shape
 * (`step`, `steps_total`) and the Signal Hub's own (`steps` = number of steps, `steps_applied`),
 * since the engine passes the hub's JSON through.
 */
export const demoPackSchema = z
  .object({
    id: z.string().regex(PACK_ID_PATTERN),
    title: z.string().min(1).max(200),
    description: z.string().max(2000).nullable().optional().catch(undefined),
    status: z.string().max(40).nullable().optional().catch(undefined),
    step: count,
    steps_total: count,
    steps: count,
    steps_applied: count,
  })
  .strip()
  .transform(({ steps, steps_applied, ...p }) => ({
    ...p,
    step: p.step ?? steps_applied ?? null,
    steps_total: p.steps_total ?? steps ?? null,
  }));
export type DemoPack = z.infer<typeof demoPackSchema>;

export const demoPacksSchema = z
  .union([
    z.array(z.unknown()),
    z.object({ items: z.array(z.unknown()) }).loose().transform((v) => v.items),
    z.object({ packs: z.array(z.unknown()) }).loose().transform((v) => v.packs),
  ])
  .transform((items) =>
    items.flatMap((i) => {
      const p = demoPackSchema.safeParse(i);
      return p.success ? [p.data] : [];
    }),
  );

/**
 * `POST /api/demo/packs/{id}/start|advance|reset`: the hub's replay state passed through by the
 * engine (`status`, `steps_applied`, `steps_total`, `next_step.note` …); every field optional.
 */
export const demoPackResultSchema = z
  .object({
    ok: z.boolean().optional().catch(undefined),
    status: z.string().max(40).nullable().optional().catch(undefined),
    step: count,
    steps_applied: count,
    steps_total: count,
    note: z.string().max(500).nullable().optional().catch(undefined),
    next_step: z.object({ note: z.string().max(500).nullable().optional() }).loose().nullable().optional().catch(undefined),
  })
  .strip()
  .transform(({ steps_applied, next_step, ...r }) => ({
    ...r,
    step: r.step ?? steps_applied ?? null,
    note: r.note ?? next_step?.note ?? null,
  }));
export type DemoPackResult = z.infer<typeof demoPackResultSchema>;

export type DemoPackAction = "start" | "advance" | "reset";

