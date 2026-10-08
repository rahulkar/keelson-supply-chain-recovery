import type { EventSourceLike } from "@/lib/stream/useCaseStream";

/** Scriptable stand-in for the browser EventSource (jsdom has none). */
export class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readyState = 0;
  closed = false;
  onopen: EventSourceLike["onopen"] = null;
  onmessage: EventSourceLike["onmessage"] = null;
  onerror: EventSourceLike["onerror"] = null;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  static reset(): void {
    FakeEventSource.instances = [];
  }

  static latest(): FakeEventSource {
    const es = FakeEventSource.instances.at(-1);
    if (!es) throw new Error("no EventSource created");
    return es;
  }

  static factory = (url: string): EventSourceLike => new FakeEventSource(url);

  open(): void {
    this.readyState = 1;
    this.onopen?.call(this as unknown as EventSource, new Event("open"));
  }

  emit(data: string, lastEventId = ""): void {
    this.onmessage?.call(this as unknown as EventSource, new MessageEvent("message", { data, lastEventId }));
  }

  /** `fatal`: the browser gave up (HTTP error / wrong content type → CLOSED). */
  fail(fatal: boolean): void {
    this.readyState = fatal ? 2 : 0;
    this.onerror?.call(this as unknown as EventSource, new Event("error"));
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }
}
