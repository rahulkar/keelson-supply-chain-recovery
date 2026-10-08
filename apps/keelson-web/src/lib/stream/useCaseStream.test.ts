import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeEventSource } from "../../../test/fakeEventSource";
import { M1_FLOW, replayDelivery, ssePayload, type RecordedEvent } from "../../../test/fixtures/recorded";
import { eventsUrl, useCaseStream, type ReceivedEvent } from "./useCaseStream";

const CASE = M1_FLOW.case_id;

function deliver(es: FakeEventSource, events: RecordedEvent[]) {
  act(() => {
    for (const e of events) es.emit(ssePayload(e), e.persist ? String(e.seq) : "");
  });
}

beforeEach(() => FakeEventSource.reset());
afterEach(() => vi.useRealTimers());

describe("useCaseStream", () => {
  it("replays the case from the start, then marks later events as live", () => {
    const seen: ReceivedEvent[] = [];
    const { result } = renderHook(() =>
      useCaseStream(CASE, { replayUntil: 57, createEventSource: FakeEventSource.factory, onEvent: (e) => seen.push(e) }),
    );
    const es = FakeEventSource.latest();
    expect(es.url).toBe(`/api/cases/${CASE}/events?after=0`);
    expect(result.current.connection).toBe("connecting");
    act(() => es.open());
    expect(result.current.connection).toBe("open");
    expect(result.current.caughtUp).toBe(false);

    const all = M1_FLOW.events;
    const cut = all.findIndex((e) => e.seq === 57) + 1;
    deliver(es, all.slice(0, cut));
    expect(result.current.caughtUp).toBe(true);
    expect(result.current.state.lastSeq).toBe(57);
    expect(seen.every((e) => !e.live)).toBe(true);

    deliver(es, all.slice(cut));
    expect(result.current.state.lastSeq).toBe(80);
    expect(result.current.state.runOrder).toHaveLength(4);
    expect(seen.slice(cut).every((e) => e.live)).toBe(true);
  });

  it("is caught up at once for a case without history", () => {
    const { result } = renderHook(() => useCaseStream(CASE, { replayUntil: 0, createEventSource: FakeEventSource.factory }));
    expect(result.current.caughtUp).toBe(true);
  });

  it("lets the browser retry transient drops (Last-Event-ID) without opening a new stream", () => {
    const { result } = renderHook(() => useCaseStream(CASE, { createEventSource: FakeEventSource.factory }));
    const es = FakeEventSource.latest();
    act(() => es.open());
    act(() => es.fail(false));
    expect(result.current.connection).toBe("reconnecting");
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(es.closed).toBe(false);
  });

  it("after a fatal drop, checks the session and reconnects from the last seq with backoff", async () => {
    vi.useFakeTimers();
    const onFatalError = vi.fn(async () => true);
    const { result } = renderHook(() =>
      useCaseStream(CASE, { createEventSource: FakeEventSource.factory, onFatalError }),
    );
    const first = FakeEventSource.latest();
    act(() => first.open());
    deliver(first, replayDelivery(M1_FLOW).filter((e) => e.seq <= 30));
    await act(async () => first.fail(true));
    expect(first.closed).toBe(true);
    expect(result.current.connection).toBe("offline");
    expect(onFatalError).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.instances).toHaveLength(1);

    await act(async () => vi.advanceTimersByTime(1000));
    const second = FakeEventSource.latest();
    expect(second).not.toBe(first);
    expect(second.url).toBe(eventsUrl(CASE, 30));
    act(() => second.open());
    // The server may overlap; duplicates are ignored.
    deliver(second, replayDelivery(M1_FLOW, 25));
    expect(result.current.state.lastSeq).toBe(80);
    expect(result.current.state.runOrder).toHaveLength(4);

    // Backoff doubles on repeated failures.
    await act(async () => second.fail(true));
    await act(async () => vi.advanceTimersByTime(1000));
    expect(FakeEventSource.instances).toHaveLength(3);
  });

  it("stops retrying when the session is gone", async () => {
    vi.useFakeTimers();
    renderHook(() => useCaseStream(CASE, { createEventSource: FakeEventSource.factory, onFatalError: async () => false }));
    const es = FakeEventSource.latest();
    await act(async () => es.fail(true));
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("drops and counts payloads that are not valid AG-UI events", () => {
    const { result } = renderHook(() => useCaseStream(CASE, { createEventSource: FakeEventSource.factory }));
    const es = FakeEventSource.latest();
    act(() => {
      es.emit("{not json");
      es.emit(JSON.stringify({ type: "NOT_A_TYPE" }));
      es.emit(JSON.stringify({ type: "STEP_STARTED", stepName: "understanding", metadata: { sc: { bad: true } } }));
    });
    expect(result.current.state.invalidEvents).toBe(3);
    expect(result.current.state.runOrder).toEqual([]);
  });

  it("an invalid event still moves the replay watermark (SSE id), so the replay can catch up", () => {
    const { result } = renderHook(() => useCaseStream(CASE, { replayUntil: 3, createEventSource: FakeEventSource.factory }));
    const es = FakeEventSource.latest();
    act(() => es.open());
    deliver(es, M1_FLOW.events.filter((e) => e.persist && e.seq <= 2));
    expect(result.current.caughtUp).toBe(false);
    act(() => es.emit(JSON.stringify({ type: "NOT_A_TYPE" }), "3"));
    expect(result.current.caughtUp).toBe(true);
    // a fatal reconnect asks only for what follows it
    act(() => es.fail(true));
    expect(result.current.state.invalidEvents).toBe(1);
  });

  it("closes the stream on unmount and when the case changes", () => {
    const { rerender, unmount } = renderHook(({ id }) => useCaseStream(id, { createEventSource: FakeEventSource.factory }), {
      initialProps: { id: CASE },
    });
    const first = FakeEventSource.latest();
    rerender({ id: "other-case" });
    expect(first.closed).toBe(true);
    const second = FakeEventSource.latest();
    expect(second.url).toBe("/api/cases/other-case/events?after=0");
    unmount();
    expect(second.closed).toBe(true);
  });

  it("does not connect while disabled", () => {
    renderHook(() => useCaseStream(CASE, { enabled: false, createEventSource: FakeEventSource.factory }));
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});
