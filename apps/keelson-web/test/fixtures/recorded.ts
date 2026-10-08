/**
 * Recorded AG-UI event fixtures (see `source` in each JSON): the exact events the engine's
 * Translator emitted for the M1 flows, with their persistence flags.
 *
 * - `liveDelivery`: what an SSE subscriber connected from the start receives: everything, in
 *   order. Live-only events (reasoning deltas) carry no seq; persisted-only events (the reasoning
 *   aggregate) reach live subscribers too, because every Valkey wake-up re-reads Postgres for
 *   `seq > last` (`core.events.EventBus.replay_then_tail`).
 * - `replayDelivery`: what a reconnect / page load replays from Postgres (persisted events only).
 */
import { parseAgUiEvent } from "@keelson/contracts";
import type { CaseStreamAction } from "@/lib/stream/reducer";
import m1Flow from "./m1-flow.events.json";
import whatIf from "./what-if.events.json";

export type RecordedEvent = {
  seq: number;
  run_id: string | null;
  persist: boolean;
  live: boolean;
  event: Record<string, unknown>;
};

export type Recording = { source: string; case_id: string; runs: [string, string][]; events: RecordedEvent[] };

export const M1_FLOW = m1Flow as unknown as Recording;
export const WHAT_IF = whatIf as unknown as Recording;

/** One reducer action per recorded event (validated exactly like the hook does). */
export function toAction(rec: RecordedEvent, live = true): CaseStreamAction {
  const parsed = parseAgUiEvent(JSON.stringify(rec.event));
  if (!parsed.ok) throw new Error(`fixture event ${rec.seq} is invalid: ${parsed.message}`);
  const seq = parsed.sc?.seq ?? null;
  return { type: "event", event: parsed.event, sc: parsed.sc, seq: seq && seq > 0 ? seq : null, live };
}

/** The SSE `data:` payload of a recorded event, with `metadata.sc.seq` as the stream sends it. */
export function ssePayload(rec: RecordedEvent): string {
  return JSON.stringify(rec.event);
}

export function liveDelivery(rec: Recording): RecordedEvent[] {
  return rec.events;
}

export function replayDelivery(rec: Recording, after = 0): RecordedEvent[] {
  return rec.events.filter((e) => e.persist && e.seq > after);
}

export function runId(rec: Recording, name: string): string {
  const found = rec.runs.find(([n]) => n === name);
  if (!found) throw new Error(`no run named ${name}`);
  return found[1];
}

/** The same recording as an engine before `ScMetadata.runId` emitted it (legacy cases). */
export function withoutRunIds(rec: Recording): Recording {
  return {
    ...rec,
    events: rec.events.map((e) => {
      const event = structuredClone(e.event) as { metadata?: { sc?: Record<string, unknown> } };
      if (event.metadata?.sc) delete event.metadata.sc["runId"];
      return { ...e, event: event as Record<string, unknown> };
    }),
  };
}
