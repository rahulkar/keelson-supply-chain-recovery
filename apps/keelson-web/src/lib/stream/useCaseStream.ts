"use client";

/**
 * `useCaseStream(caseId)` — follows `GET /api/cases/{id}/events` (AG-UI 1.0 over SSE).
 *
 * - The first connection replays the whole case (`?after=0`) so every past turn gets its timeline.
 * - Transient drops: the browser's EventSource reconnects by itself and sends `Last-Event-ID`
 *   (the server prefers it over `?after=`), so only missing events are replayed.
 * - Fatal drops (HTTP error, wrong content type → `readyState === CLOSED`): we check the session
 *   (a 401 sends the user to sign-in) and otherwise reconnect with backoff from `?after=<lastSeq>`.
 * - Every payload is validated with `parseAgUiEvent`; invalid events are counted and dropped.
 * - Deduplication by `metadata.sc.seq` happens in the reducer (overlapping replays are harmless).
 */
import { parseAgUiEvent, type AgUiEvent, type ScMetadata } from "@keelson/contracts";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { caseStreamReducer, initialCaseStreamState, type CaseStreamAction, type CaseStreamState } from "./reducer";

export type StreamConnection = "connecting" | "open" | "reconnecting" | "offline";

export type EventSourceLike = Pick<EventSource, "close" | "readyState"> & {
  onopen: ((this: EventSource, ev: Event) => unknown) | null;
  onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null;
  onerror: ((this: EventSource, ev: Event) => unknown) | null;
};

export type UseCaseStreamOptions = {
  /** Replay is "caught up" once this seq has been applied (`last_event_seq` of the snapshot). */
  replayUntil?: number | undefined;
  enabled?: boolean | undefined;
  /** Test seam. */
  createEventSource?: ((url: string) => EventSourceLike) | undefined;
  /** Called when the stream fails for good; resolve `false` to stop retrying (e.g. signed out). */
  onFatalError?: (() => Promise<boolean>) | undefined;
  /** Called for every valid event after it was folded into the state (e.g. to auto-open the canvas). */
  onEvent?: ((received: ReceivedEvent) => void) | undefined;
};

export type ReceivedEvent = {
  event: AgUiEvent;
  sc: ScMetadata | null;
  seq: number | null;
  /** Arrived after the initial replay caught up. */
  live: boolean;
};

export type UseCaseStreamResult = {
  state: CaseStreamState;
  dispatch: (action: CaseStreamAction) => void;
  connection: StreamConnection;
  /** The initial replay has reached `replayUntil`. */
  caughtUp: boolean;
};

const CLOSED = 2;
const MAX_BACKOFF_MS = 30_000;

export function eventsUrl(caseId: string, after: number): string {
  return `/api/cases/${encodeURIComponent(caseId)}/events?after=${Math.max(0, Math.floor(after))}`;
}

function defaultEventSource(url: string): EventSourceLike {
  return new EventSource(url, { withCredentials: true });
}

/** `metadata.sc.seq` of a persisted event, else `null` (live-only event). */
function persistedSeq(seq: number | null | undefined): number | null {
  return typeof seq === "number" && Number.isInteger(seq) && seq > 0 ? seq : null;
}

export function useCaseStream(caseId: string, options: UseCaseStreamOptions = {}): UseCaseStreamResult {
  const { replayUntil = 0, enabled = true, createEventSource = defaultEventSource, onFatalError, onEvent } = options;
  const [state, dispatch] = useReducer(caseStreamReducer, initialCaseStreamState);
  const [connection, setConnection] = useState<StreamConnection>("connecting");
  const [caughtUp, setCaughtUp] = useState(replayUntil <= 0);

  const lastSeq = useRef(0);
  const caughtUpRef = useRef(replayUntil <= 0);
  const replayUntilRef = useRef(replayUntil);
  const factoryRef = useRef(createEventSource);
  const fatalRef = useRef(onFatalError);
  const onEventRef = useRef(onEvent);
  useEffect(() => {
    factoryRef.current = createEventSource;
    fatalRef.current = onFatalError;
    onEventRef.current = onEvent;
  });

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let source: EventSourceLike | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let backoff = 1000;

    lastSeq.current = 0;
    caughtUpRef.current = replayUntilRef.current <= 0;
    dispatch({ type: "reset" });
    setCaughtUp(caughtUpRef.current);

    const markCaughtUp = () => {
      if (caughtUpRef.current) return;
      caughtUpRef.current = true;
      setCaughtUp(true);
    };

    const connect = () => {
      if (disposed) return;
      setConnection(lastSeq.current > 0 ? "reconnecting" : "connecting");
      const es = factoryRef.current(eventsUrl(caseId, lastSeq.current));
      source = es;

      es.onopen = () => {
        if (disposed || source !== es) return;
        backoff = 1000;
        setConnection("open");
        // Nothing to replay beyond what the snapshot knew: the replay is trivially complete.
        if (lastSeq.current >= replayUntilRef.current) markCaughtUp();
      };

      es.onmessage = (msg: MessageEvent) => {
        if (disposed || source !== es) return;
        const parsed = parseAgUiEvent(typeof msg.data === "string" ? msg.data : "");
        if (!parsed.ok) {
          dispatch({ type: "invalid_event" });
          // Still count it towards the replay watermark (SSE `id` = seq), so a malformed event at
          // the end of the history can neither stall "caught up" nor be re-requested forever.
          const sseSeq = persistedSeq(Number(msg.lastEventId || NaN));
          if (sseSeq !== null && sseSeq > lastSeq.current) lastSeq.current = sseSeq;
          if (!caughtUpRef.current && lastSeq.current >= replayUntilRef.current) markCaughtUp();
          return;
        }
        const seq = persistedSeq(parsed.sc?.seq);
        const live = caughtUpRef.current;
        if (seq !== null && seq > lastSeq.current) lastSeq.current = seq;
        dispatch({ type: "event", event: parsed.event, sc: parsed.sc, seq, live });
        onEventRef.current?.({ event: parsed.event, sc: parsed.sc, seq, live });
        if (!caughtUpRef.current && lastSeq.current >= replayUntilRef.current) markCaughtUp();
      };

      es.onerror = () => {
        if (disposed || source !== es) return;
        if (es.readyState !== CLOSED) {
          // The browser retries on its own with Last-Event-ID.
          setConnection("reconnecting");
          return;
        }
        es.close();
        source = null;
        setConnection("offline");
        const delay = backoff;
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        void (async () => {
          const keepGoing = fatalRef.current ? await fatalRef.current().catch(() => true) : true;
          if (disposed || !keepGoing) return;
          retryTimer = setTimeout(connect, delay);
        })();
      };
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      source?.close();
      source = null;
    };
  }, [caseId, enabled]);

  // A snapshot refresh may raise the replay target after mount.
  useEffect(() => {
    replayUntilRef.current = replayUntil;
    if (!caughtUpRef.current && lastSeq.current >= replayUntil && connection === "open") {
      caughtUpRef.current = true;
      setCaughtUp(true);
    }
  }, [replayUntil, connection]);

  const stableDispatch = useCallback((action: CaseStreamAction) => dispatch(action), []);
  return { state, dispatch: stableDispatch, connection, caughtUp };
}
