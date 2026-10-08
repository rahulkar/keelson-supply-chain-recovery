"use client";

import { blockKey, formatIssues, hasBlockData, parseBlockData, toBlockEnvelope, type BlockEnvelope } from "@keelson/contracts";
import { Skeleton } from "@keelson/ui";
import { Component, useEffect, useMemo, useRef, useState, type ErrorInfo, type ReactNode } from "react";
import { RawDataCard } from "./blocks/RawDataCard";
import { getBlockDefinition, type BlockDefinition } from "./registry";
import type { BlockDataLoader, BlockElement, BlockRendererProps } from "./types";

type BoundaryProps = { children: ReactNode; fallback: (error: Error) => ReactNode; resetKey: unknown };
type BoundaryState = { error: Error | null; resetKey: unknown };

/** Contains render errors of a single block so one bad card never takes down the thread. */
class BlockErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  override state: BoundaryState = { error: null, resetKey: undefined };

  static getDerivedStateFromError(error: Error): Partial<BoundaryState> {
    return { error };
  }

  static getDerivedStateFromProps(props: BoundaryProps, state: BoundaryState): Partial<BoundaryState> | null {
    // A new envelope (block replaced via ACTIVITY_SNAPSHOT) gets a fresh chance to render.
    if (props.resetKey !== state.resetKey) return { error: null, resetKey: props.resetKey };
    return null;
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    if (typeof console !== "undefined") console.error("[keelson/viz] block render failed", error, info.componentStack);
  }

  override render(): ReactNode {
    return this.state.error ? this.props.fallback(this.state.error) : this.props.children;
  }
}

/**
 * Legacy `data_ref` on kinds without a data schema (M1): the payload is merged over `props`
 * (shallow) before validation; anything that is not an object is offered as `props.data`.
 */
export function mergeBlockData(envelope: BlockEnvelope, data: unknown): BlockEnvelope {
  const base = envelope.props && typeof envelope.props === "object" && !Array.isArray(envelope.props) ? envelope.props : {};
  const extra = data && typeof data === "object" && !Array.isArray(data) ? (data as Record<string, unknown>) : { data };
  return { ...envelope, props: { ...base, ...extra } };
}

type PayloadState =
  | { status: "loading" }
  | { status: "error"; message: string; retry: () => void }
  | { status: "ready"; data: unknown; refreshing?: boolean };

type Settled =
  | { key: string; blockId: string; status: "error"; message: string }
  | { key: string; blockId: string; status: "ready"; data: unknown };

/**
 * Resolves a block's `data_ref` payload with `loadData` (or takes the given `data`). Re-fetches
 * when the block id, its `data_ref` or `dataKey` change; a stale answer is ignored (abort).
 * While a re-emitted block (new `dataKey`) loads again, the payload already shown stays on
 * screen (`refreshing`), so the visual keeps its state (view, filter, map position) instead of
 * flashing back to a skeleton. `null` when there is nothing to load (no `data_ref` or no loader).
 */
function useBlockPayload(
  header: BlockEnvelope,
  loadData: BlockDataLoader | undefined,
  given: { has: boolean; data: unknown },
  dataKey: string | number | undefined,
): PayloadState | null {
  const [attempt, setAttempt] = useState(0);
  const key = `${header.id}|${header.data_ref ?? ""}|${dataKey ?? ""}#${attempt}`;
  const [settled, setSettled] = useState<Settled | null>(null);
  const blockId = header.id;
  const loader = useRef(loadData);
  const envelopeRef = useRef(header);
  useEffect(() => {
    loader.current = loadData;
    envelopeRef.current = header;
  });
  const canLoad = !given.has && !!header.data_ref && !!loadData;

  useEffect(() => {
    const load = loader.current;
    if (!canLoad || !load) return;
    const ctrl = new AbortController();
    load(envelopeRef.current, { signal: ctrl.signal })
      .then((data) => {
        if (!ctrl.signal.aborted) setSettled({ key, blockId, status: "ready", data });
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        setSettled({ key, blockId, status: "error", message: err instanceof Error && err.message ? err.message : "Request failed" });
      });
    return () => ctrl.abort();
  }, [canLoad, key, blockId]);

  if (given.has) return { status: "ready", data: given.data };
  if (!canLoad) return null;
  if (!settled) return { status: "loading" };
  if (settled.key !== key) {
    // Same block, newer revision: keep the payload on screen until the new one arrives.
    if (settled.status === "ready" && settled.blockId === blockId) return { status: "ready", data: settled.data, refreshing: true };
    return { status: "loading" };
  }
  if (settled.status === "error") return { status: "error", message: settled.message, retry: () => setAttempt((n) => n + 1) };
  return { status: "ready", data: settled.data };
}

type ResolvedProps = Omit<BlockRendererProps, "envelope"> & {
  header: BlockEnvelope;
  rawEnvelope: unknown;
  definition: BlockDefinition<unknown>;
  blockKeyStr: `${string}@${number}`;
};

function renderBlock(
  { onAction, busy = false, density = "inline", viewer, decision, rawEnvelope, definition, blockKeyStr }: ResolvedProps,
  header: BlockEnvelope,
  props: unknown,
  data: unknown,
): BlockElement {
  const Block = definition.component;
  return (
    <BlockErrorBoundary
      resetKey={rawEnvelope}
      fallback={(error) => (
        <RawDataCard data={header.props} title={header.title} blockKey={blockKeyStr} reason="render_error" issues={[error.message]} />
      )}
    >
      <Block
        props={props}
        data={data}
        envelope={header}
        onAction={onAction}
        busy={busy}
        density={density}
        viewer={viewer}
        decision={decision}
      />
    </BlockErrorBoundary>
  );
}

function loadingCard(header: BlockEnvelope): BlockElement {
  return <Skeleton className="h-28 w-full rounded-lg" label={`Loading ${header.title || "result"}`} />;
}

/** Kinds with a `data_ref` payload (M3 visuals): props validated first, then the payload. */
function DataBlock(p: ResolvedProps & { props: unknown }): BlockElement {
  const { header, loadData, data, dataKey, blockKeyStr } = p;
  const payload = useBlockPayload(header, loadData, { has: data !== undefined, data }, dataKey);
  const ready = payload?.status === "ready" ? payload.data : undefined;
  const hasReady = payload?.status === "ready";
  // Validated once per payload (not on every parent render): a stable `data` object keeps the
  // visuals' memoised models, deck.gl layers, ELK layout and chart options from being rebuilt.
  const parsed = useMemo(
    () => (hasReady ? parseBlockData({ kind: header.kind, v: header.v }, ready) : null),
    [hasReady, ready, header.kind, header.v],
  );
  if (!payload) {
    return (
      <RawDataCard
        data={header.props}
        title={header.title}
        blockKey={blockKeyStr}
        reason="data_unavailable"
        issues={[header.data_ref ? "No data loader is available here" : "This block has no data_ref"]}
      />
    );
  }
  if (payload.status === "loading") return loadingCard(header);
  if (payload.status === "error") {
    return (
      <RawDataCard
        data={header.props}
        title={header.title}
        blockKey={blockKeyStr}
        reason="data_unavailable"
        issues={[payload.message]}
        onRetry={payload.retry}
      />
    );
  }
  if (!parsed) return loadingCard(header);
  if (!parsed.ok) {
    return (
      <RawDataCard data={payload.data} title={header.title} blockKey={blockKeyStr} reason="invalid_data" issues={parsed.issues} />
    );
  }
  return renderBlock(p, header, p.props, parsed.data);
}

/** M1 kinds that still carry a `data_ref`: payload merged over props, then validated. */
function MergedBlock(p: ResolvedProps): BlockElement {
  const { header, loadData, data, dataKey, blockKeyStr, definition } = p;
  const payload = useBlockPayload(header, loadData, { has: data !== undefined, data }, dataKey);
  let resolved = header;
  if (payload?.status === "loading") return loadingCard(header);
  if (payload?.status === "ready") resolved = mergeBlockData(header, payload.data);
  return validateAndRender(p, resolved, definition, blockKeyStr);
}

type PropsParse = ReturnType<BlockDefinition<unknown>["schema"]["safeParse"]>;

function validateAndRender(
  p: ResolvedProps,
  header: BlockEnvelope,
  definition: BlockDefinition<unknown>,
  key: `${string}@${number}`,
  parsedProps?: PropsParse,
): BlockElement {
  const parsed = parsedProps ?? definition.schema.safeParse(header.props);
  if (!parsed.success) {
    return (
      <RawDataCard data={header.props} title={header.title} blockKey={key} reason="invalid_props" issues={formatIssues(parsed.error)} />
    );
  }
  return renderBlock(p, header, parsed.data, undefined);
}

/**
 * Renders one render-block envelope. Validates `envelope.props` with the Zod schema for its
 * `kind@v`; kinds with a `data_ref` payload get it resolved (`loadData`) and validated before
 * they render. Unknown kinds, invalid props / data and render errors fall back to {@link RawDataCard}.
 */
export function BlockRenderer(rendererProps: BlockRendererProps): BlockElement {
  const { envelope } = rendererProps;
  // Parsed once per envelope object (not on every parent re-render, e.g. each stream event):
  // blocks get the same `props` object back, so their memoised charts / layouts stay put.
  const header = useMemo(() => toBlockEnvelope(envelope), [envelope]);
  const key = header ? blockKey(header.kind, header.v) : null;
  const definition = key ? getBlockDefinition(key) : undefined;
  const parsedProps = useMemo(
    () => (header && definition ? definition.schema.safeParse(header.props) : undefined),
    [header, definition],
  );
  if (!header || !key) return <RawDataCard data={envelope} reason="not_an_envelope" />;

  if (!definition) {
    return (
      <RawDataCard
        data={header.props}
        title={header.title}
        blockKey={key}
        reason="unknown_kind"
        issues={[`No renderer for "${key}"`]}
      />
    );
  }

  const resolved: ResolvedProps = { ...rendererProps, header, rawEnvelope: envelope, definition, blockKeyStr: key };

  if (hasBlockData(key)) {
    const parsed = parsedProps ?? definition.schema.safeParse(header.props);
    if (!parsed.success) {
      return (
        <RawDataCard data={header.props} title={header.title} blockKey={key} reason="invalid_props" issues={formatIssues(parsed.error)} />
      );
    }
    return <DataBlock key={header.id} {...resolved} props={parsed.data} />;
  }

  if (header.data_ref && (rendererProps.loadData || rendererProps.data !== undefined)) {
    return <MergedBlock key={header.id} {...resolved} />;
  }
  return validateAndRender(resolved, header, definition, key, parsedProps);
}
