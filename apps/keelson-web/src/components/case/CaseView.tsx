"use client";

import { EventType, renderBlockFromEvent, type BlockEnvelope } from "@keelson/contracts";
import {
  Badge,
  CANVAS_SIDE_QUERY,
  CanvasPanel,
  Composer,
  EmptyState,
  IconButton,
  Skeleton,
  toast,
  useMediaQuery,
} from "@keelson/ui";
import { undoAction, type BlockViewer } from "@keelson/viz";
import { FileSearch, LayoutPanelLeft, PanelRight } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isApiError } from "@/lib/api/client";
import type { CaseDetail } from "@/lib/api/types";
import { useSession } from "@/lib/client/session";
import { useCaseActions } from "@/lib/client/useCaseActions";
import { connectReturnOutcome, useConnectorLink } from "@/lib/client/useConnectorLink";
import { interruptDecision } from "@/lib/decisions";
import { canWriteCases, hasAnyRole } from "@/lib/roles";
import { selectOpenInterruptIds, selectRunningRun } from "@/lib/stream/reducer";
import { useCaseStream, type ReceivedEvent, type UseCaseStreamOptions } from "@/lib/stream/useCaseStream";
import { markCaseSeen } from "@/lib/seen";
import {
  buildTurns,
  canvasBlocks,
  followUpContext,
  followUps,
  hasActiveWork,
  latestBanner,
  planVersionViews,
  staleQueuedRuns,
  type ThreadMessage,
} from "@/lib/thread";
import { CaseBlock } from "./CaseBlock";
import { PlanVersionsDialog } from "./PlanVersionsDialog";
import { TraceView } from "./TraceView";
import { TurnView, type BlockGate } from "./TurnView";

type CanvasTabId = "result" | "evidence" | "trace";

const AUTO_OPEN_KEY = "keelson-canvas-auto:";

function readAutoOpened(caseId: string): boolean {
  try {
    return window.localStorage.getItem(AUTO_OPEN_KEY + caseId) === "1";
  } catch {
    return false;
  }
}

function rememberAutoOpened(caseId: string): void {
  try {
    window.localStorage.setItem(AUTO_OPEN_KEY + caseId, "1");
  } catch {
    // storage blocked: auto-open may happen again on reload, which is harmless
  }
}

function propsOf(envelope: BlockEnvelope): Record<string, unknown> {
  return envelope.props && typeof envelope.props === "object" ? (envelope.props as Record<string, unknown>) : {};
}

export type CaseViewProps = {
  initial: CaseDetail;
  /** Test seam for the event stream. */
  streamOptions?: Pick<UseCaseStreamOptions, "createEventSource"> | undefined;
};

/**
 * The case thread: one turn per run (user bubble → thinking timeline → narrative → ≤ 2 inline
 * blocks → follow-ups), the composer, and the Canvas (Result / Evidence / Trace).
 */
export function CaseView({ initial, streamOptions }: CaseViewProps) {
  const { me, api } = useSession();
  const caseId = initial.case.id;
  const canWrite = canWriteCases(me.user);

  const [snapshot, setSnapshot] = useState(initial);
  const [local, setLocal] = useState<ThreadMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [canvasTab, setCanvasTab] = useState<CanvasTabId>("result");
  const wide = useMediaQuery(CANVAS_SIDE_QUERY, true);
  const canvasToggle = useRef<HTMLButtonElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const focusBlock = useRef<string | null>(null);
  /** Plan version switcher (M5): open + which version to show first. */
  const [versionsAt, setVersionsAt] = useState<{ open: boolean; version?: number | undefined }>({ open: false });
  const toastedBanners = useRef(new Set<string>());

  // "Updated" badge in the case list: this viewer has seen the case as of now.
  useEffect(() => {
    markCaseSeen(caseId);
    return () => markCaseSeen(caseId);
  }, [caseId]);

  /**
   * Re-read the case snapshot (user messages, pending interrupts). Responses can arrive out of
   * order: only the newest request may win, and never with an older `last_event_seq`.
   */
  const refreshSeq = useRef(0);
  const appliedSeq = useRef(0);
  const refresh = useCallback(() => {
    const request = ++refreshSeq.current;
    api
      .getCase(caseId)
      .then((next) => {
        if (request < appliedSeq.current) return;
        appliedSeq.current = request;
        setSnapshot((prev) => (next.last_event_seq >= prev.last_event_seq ? next : prev));
      })
      .catch(() => {
        // the stream keeps the thread current; a failed refresh only delays messages / interrupts
      });
  }, [api, caseId]);

  const onFatalError = useCallback(async () => {
    try {
      await api.me();
      return true;
    } catch (err) {
      return !(isApiError(err) && err.status === 401);
    }
  }, [api]);

  const onEvent = useCallback(
    ({ event, live }: ReceivedEvent) => {
      if (!live) return;
      markCaseSeen(caseId);
      // A run started or ended: re-read the snapshot. It is the only source that says whether a
      // pending interrupt was resolved / superseded, and it carries user messages posted elsewhere.
      if (event.type === EventType.RUN_STARTED || event.type === EventType.RUN_FINISHED || event.type === EventType.RUN_ERROR) {
        refresh();
        return;
      }
      const envelope = renderBlockFromEvent(event);
      // A re-plan arrived while this case is open (M5 §4): say so once per narrative block.
      if (envelope?.kind === "narrative") {
        const banner = (propsOf(envelope)["banner"] ?? null) as { text?: unknown; plan_version?: unknown } | null;
        if (banner && typeof banner.text === "string" && !toastedBanners.current.has(envelope.id)) {
          toastedBanners.current.add(envelope.id);
          const version = typeof banner.plan_version === "number" ? banner.plan_version : undefined;
          toast(banner.text, {
            timeout: 10_000,
            action: { label: "Compare versions", onClick: () => setVersionsAt({ open: true, version }) },
          });
        }
      }
      // Canvas auto-opens on the first heavy result that arrives live (wide screens, once per case).
      if (!envelope || envelope.placement !== "canvas" || envelope.kind === "evidence_pack") return;
      if (!window.matchMedia?.(CANVAS_SIDE_QUERY).matches || readAutoOpened(caseId)) return;
      rememberAutoOpened(caseId);
      focusBlock.current = envelope.id;
      setCanvasTab("result");
      setCanvasOpen(true);
    },
    [caseId, refresh],
  );

  const { state, dispatch, connection, caughtUp } = useCaseStream(caseId, {
    replayUntil: snapshot.last_event_seq,
    onFatalError,
    onEvent,
    ...streamOptions,
  });

  // ── messages: server snapshot + the ones posted from this tab ──
  const messages = useMemo<ThreadMessage[]>(() => {
    const known = new Set(snapshot.messages.map((m) => m.run_id).filter(Boolean));
    return [...snapshot.messages, ...local.filter((m) => !m.run_id || !known.has(m.run_id))];
  }, [snapshot.messages, local]);

  // Runs the snapshot knows about (user messages) are announced before the replay arrives, so a
  // posted message shows as a queued turn at once (its RUN_ERROR, if cancelled before it started,
  // names it in metadata.sc.runId).
  useEffect(() => {
    for (const m of messages) {
      if (m.role === "user" && m.run_id) dispatch({ type: "expect_run", runId: m.run_id });
    }
  }, [messages, dispatch]);
  // ... and so are queued runs no message names (a case opened from an alert: its signal run).
  useEffect(() => {
    for (const r of snapshot.pending_runs ?? []) dispatch({ type: "expect_run", runId: r.id });
  }, [snapshot.pending_runs, dispatch]);

  // Which interrupts are still pending comes from the snapshot (as of its last_event_seq).
  useEffect(() => {
    dispatch({
      type: "server_interrupts",
      openIds: snapshot.open_interrupts.map((i) => i.id),
      asOfSeq: snapshot.last_event_seq,
    });
  }, [snapshot.open_interrupts, snapshot.last_event_seq, dispatch]);

  const turns = useMemo(() => buildTurns(messages, state), [messages, state]);
  const stale = useMemo(() => staleQueuedRuns(messages, state), [messages, state]);
  const running = selectRunningRun(state);
  const activeWork = hasActiveWork(state, stale);
  const canvas = useMemo(() => canvasBlocks(state), [state]);
  const banner = useMemo(() => latestBanner(state), [state]);
  const versions = useMemo(() => planVersionViews(state), [state]);

  // ── actions ──
  const openCanvasOn = useCallback((blockId: string | null, tab: CanvasTabId = "result") => {
    focusBlock.current = blockId;
    setCanvasTab(tab);
    setCanvasOpen(true);
  }, []);

  const { sendMessage, cancelRun, onBlockAction } = useCaseActions({
    api,
    caseId,
    dispatch,
    refresh,
    openCanvas: (blockId) => openCanvasOn(blockId),
    onMessageAccepted: ({ text, runId, key }) =>
      setLocal((prev) => [
        ...prev,
        { id: `local-${key}`, role: "user", text, run_id: runId, created_at: new Date().toISOString() },
      ]),
  });

  /** Synchronous guard: a second submit before `sending` re-renders must not post twice. */
  const sendingRef = useRef(false);
  const send = async (text: string) => {
    if (sendingRef.current || !canWrite) return;
    sendingRef.current = true;
    setSending(true);
    const ok = await sendMessage(text);
    sendingRef.current = false;
    setSending(false);
    if (ok) setDraft("");
  };

  // ── gates for block actions ──
  const openInterrupts = useMemo(() => selectOpenInterruptIds(state), [state]);

  const latestPlanVersion = useMemo(() => {
    let v: number | null = null;
    for (const b of Object.values(state.blocks)) {
      const pv = propsOf(b.envelope)["plan_version"];
      if (b.envelope.kind === "recommended_option" && typeof pv === "number") v = v === null ? pv : Math.max(v, pv);
    }
    return v;
  }, [state.blocks]);

  const approvalPending = Object.values(state.interrupts).some((i) => i.open && i.reason === "approval");
  /** A commit tracker was emitted after this block (its plan was applied). */
  const appliedAfter = (blockId: string) => {
    const at = state.blockOrder.indexOf(blockId);
    return state.blockOrder.some((id, i) => i > at && state.blocks[id]?.envelope.kind === "commit_tracker");
  };

  // Back from the Connect-Merxa flow (`?connected=merxa` / `?connect_error=…`): say so once and
  // drop the parameters (a reload must not repeat it). The link itself is re-read on this load.
  useEffect(() => {
    const back = connectReturnOutcome(window.location.href);
    if (!back) return;
    window.history.replaceState(window.history.state, "", back.cleanUrl);
    if (back.outcome.ok) toast.success(`${back.outcome.connector} connected`, { description: "You can approve changes now." });
    else toast.error("Couldn't connect Merxa", { description: back.outcome.message });
  }, []);

  const decidedById = useMemo(() => new Map((snapshot.decided_interrupts ?? []).map((d) => [d.id, d])), [snapshot.decided_interrupts]);

  const gate: BlockGate = (envelope) => {
    const props = propsOf(envelope);
    if (envelope.kind === "approach_card" || envelope.kind === "approval_card") {
      const id = props["interrupt_id"];
      const open = typeof id === "string" && openInterrupts.has(id);
      // Decided / closed cards say who decided (for every viewer) instead of showing dead buttons.
      const decision =
        typeof id === "string"
          ? interruptDecision({ open, entry: state.interrupts[id], server: decidedById.get(id), viewerName: me.user.name })
          : undefined;
      // M5: the quorum's roles (four-eyes: approver + compliance) when the card carries one.
      const quorum = props["quorum"] as { required?: { roles?: unknown } } | null | undefined;
      const listed = Array.isArray(quorum?.required?.roles) ? quorum.required.roles : props["required_roles"];
      const roles =
        envelope.kind === "approval_card" && Array.isArray(listed) ? listed.filter((r): r is string => typeof r === "string") : [];
      // A quorum role may decide an approval even without case write access (compliance co-approves;
      // the decision API checks the interrupt's quorum roles, not the write roles).
      const mayDecide = canWrite || (roles.length > 0 && hasAnyRole(me.user, roles));
      if (!mayDecide) return { disabled: true, decision };
      if (!open) return { disabled: true, decision };
      if (roles.length > 0 && !hasAnyRole(me.user, roles)) {
        return { disabled: true, note: `Waiting for someone with the ${roles.join(" or ")} role to decide.` };
      }
      return { disabled: false };
    }
    if (!canWrite) return { disabled: true };
    // Ticking a guide step starts no run, so it never waits for one (M5 §2).
    if (envelope.kind === "remediation_checklist") return { disabled: false };
    // Undo (M5 §3) waits while an approval is open: a rollback is always approved by people, so a
    // requested undo sits at that approval with the change set still "applied" — a second click
    // would queue another rollback of the same change set.
    if (envelope.kind === "commit_tracker" && approvalPending) {
      const offersUndo = Array.isArray(props["actions"]) && undoAction(props as Parameters<typeof undoAction>[0]) !== null;
      return offersUndo ? { disabled: true, note: "Undo is available again once the pending approval is decided." } : { disabled: true };
    }
    if (envelope.kind === "recommended_option") {
      const pv = props["plan_version"];
      if (typeof pv === "number" && latestPlanVersion !== null && pv < latestPlanVersion) {
        return { disabled: true, note: "A newer plan replaced this one." };
      }
      if (approvalPending) return { disabled: true, note: "The changes for this plan are waiting for approval." };
      if (appliedAfter(envelope.id)) return { disabled: true, note: `These changes were applied in ${me.brand.vendor}.` };
    }
    return { disabled: activeWork };
  };

  // The viewer's own Merxa link decides Approve vs "Connect Merxa" on approval cards they can act
  // on (the card's `connect` prop is the initiator's). Checked only while such a card is shown.
  const needsMerxaLink = Object.values(state.blocks).some(
    (b) => b.envelope.kind === "approval_card" && !gate(b.envelope).disabled,
  );
  const merxaLink = useConnectorLink(api, "merxa", needsMerxaLink);
  const viewer = useMemo<BlockViewer>(
    () => ({ connections: { merxa: merxaLink }, sub: me.user.sub, roles: me.user.roles }),
    [merxaLink, me.user.sub, me.user.roles],
  );

  // Focus the requested block once the canvas shows it (CanvasPanel returns focus on close).
  useEffect(() => {
    if (!canvasOpen || !focusBlock.current) return;
    const id = focusBlock.current;
    const raf = requestAnimationFrame(() => {
      const el = document.getElementById(`block-${id}`);
      if (el) {
        el.focus({ preventScroll: true });
        el.scrollIntoView({ block: "start", behavior: "smooth" });
        focusBlock.current = null;
      }
    });
    return () => cancelAnimationFrame(raf);
  }, [canvasOpen, canvasTab, state.blockOrder]);


  // ── keep the newest turn in view while the user is at the bottom ──
  const pinned = useRef(true);
  const onScroll = () => {
    const el = scroller.current;
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };
  useEffect(() => {
    if (pinned.current) bottom.current?.scrollIntoView({ block: "end" });
  }, [turns]);

  const title = state.caseMeta?.title || snapshot.case.title || "Case";
  const mode = state.caseMeta?.mode ?? snapshot.case.mode;
  const status = state.caseMeta?.status ?? snapshot.case.status;
  const replaying = !caughtUp && snapshot.last_event_seq > 0 && state.runOrder.length === 0;
  const last = turns.at(-1);
  const lastFollowUps =
    last && canWrite && !activeWork && !running && !last.stale ? followUps(last, followUpContext(state)) : [];
  const hasCanvas =
    canvas.result.length + canvas.evidence.length > 0 || state.runOrder.some((id) => state.runs[id]?.status !== "queued");

  const canvasTabs = [
    { id: "result", label: "Result", badge: canvas.result.length || undefined },
    { id: "evidence", label: "Evidence", badge: canvas.evidence.length || undefined },
    { id: "trace", label: "Trace" },
  ];

  return (
    // Clipped: both children scroll on their own, and the canvas slides in from the right, which
    // otherwise made the whole page scroll sideways for a moment (scrollbar flash).
    <div className="flex min-h-0 flex-1 overflow-hidden" data-testid="case-layout">
      <div className="flex min-w-0 flex-1 flex-col">
        <div ref={scroller} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex w-full max-w-chat flex-col gap-8 px-4 pt-4 pb-8">
            <header className="flex items-start gap-3">
              <div className="min-w-0 flex-1">
                <h1 className="text-heading font-semibold text-fg">{title}</h1>
                <div className="mt-1 flex flex-wrap gap-2">
                  {mode === "what_if" ? <Badge tone="info">What-if</Badge> : null}
                  {status === "awaiting_approval" ? <Badge tone="warning">Awaiting approval</Badge> : null}
                  {banner ? <Badge tone="info">Updated · Plan v{banner.plan_version}</Badge> : null}
                  {versions.length > 1 ? (
                    <button
                      type="button"
                      onClick={() => setVersionsAt({ open: true })}
                      className="rounded-pill text-caption font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
                    >
                      Plan versions ({versions.length})
                    </button>
                  ) : null}
                </div>
              </div>
              {hasCanvas ? (
                <IconButton
                  ref={canvasToggle}
                  label={canvasOpen ? "Close canvas" : "Open canvas"}
                  aria-expanded={canvasOpen}
                  icon={wide ? <PanelRight className="size-5" /> : <LayoutPanelLeft className="size-5" />}
                  onClick={() => setCanvasOpen(!canvasOpen)}
                />
              ) : null}
            </header>

            {replaying ? (
              <div className="flex flex-col gap-4">
                <Skeleton className="ml-auto h-10 w-2/3 rounded-xl" />
                <Skeleton lines={3} label="Loading the case" />
              </div>
            ) : null}

            {turns.map((turn, index) => {
              const run = turn.run;
              // A queued run can be stopped once the history is replayed (until then every past
              // run still looks queued). Its RUN_ERROR names it (metadata.sc.runId), so stopping
              // it never touches a run that is in progress.
              const stoppable =
                canWrite && !!run && (run.status === "running" || (run.status === "queued" && !turn.stale && caughtUp));
              return (
                <TurnView
                  key={turn.key}
                  turn={turn}
                  index={index}
                  interrupts={state.interrupts}
                  assistantName={me.brand.engine}
                  gate={gate}
                  viewer={viewer}
                  onAction={onBlockAction}
                  onStop={stoppable && run ? () => void cancelRun(run.id) : undefined}
                  followUps={turn === last ? lastFollowUps : []}
                  onFollowUp={(prompt) => void send(prompt)}
                  followUpsDisabled={sending}
                  onComparePlans={(version) => setVersionsAt({ open: true, version })}
                />
              );
            })}
            <div ref={bottom} aria-hidden="true" />
          </div>
        </div>

        <div className="shrink-0 px-4 pt-2 pb-4">
          <div className="mx-auto flex w-full max-w-chat flex-col gap-2">
            {connection === "reconnecting" || connection === "offline" ? (
              <p role="status" className="text-center text-caption text-fg-muted">
                Connection lost. Reconnecting…
              </p>
            ) : null}
            {canWrite ? (
              <Composer
                value={draft}
                onChange={setDraft}
                onSubmit={(text) => void send(text)}
                busy={sending || !!running}
                onStop={running ? () => void cancelRun(running.id) : undefined}
                placeholder="Ask a follow-up or describe what changed…"
                label="Message"
              />
            ) : (
              <p className="rounded-xl border border-border bg-surface px-5 py-3 text-ui text-fg-muted">
                Your role can follow this case but not change it.
              </p>
            )}
          </div>
        </div>
      </div>

      <PlanVersionsDialog
        open={versionsAt.open}
        onOpenChange={(open) => setVersionsAt((prev) => ({ ...prev, open }))}
        versions={versions}
        initialVersion={versionsAt.version}
      />

      <CanvasPanel
        open={canvasOpen}
        onOpenChange={setCanvasOpen}
        returnFocusRef={canvasToggle}
        tabs={canvasTabs}
        activeTab={canvasTab}
        onTabChange={(id) => setCanvasTab(id as CanvasTabId)}
      >
        {canvasTab === "result" ? (
          canvas.result.length > 0 ? (
            <div className="flex flex-col gap-4 pt-2">
              {canvas.result.map((b) => (
                <CaseBlock
                  key={b.envelope.id}
                  envelope={b.envelope}
                  revision={b.revision}
                  onAction={onBlockAction}
                  busy={gate(b.envelope).disabled}
                  density="canvas"
                  viewer={viewer}
                  decision={gate(b.envelope).decision}
                />
              ))}
            </div>
          ) : (
            <EmptyState icon={<FileSearch />} title="No results yet" description="Maps, plans and change lists appear here." />
          )
        ) : null}
        {canvasTab === "evidence" ? (
          canvas.evidence.length > 0 ? (
            <div className="flex flex-col gap-4 pt-2">
              {canvas.evidence.map((b) => (
                <CaseBlock
                  key={b.envelope.id}
                  envelope={b.envelope}
                  revision={b.revision}
                  onAction={onBlockAction}
                  busy
                  density="canvas"
                />
              ))}
            </div>
          ) : (
            <EmptyState icon={<FileSearch />} title="No evidence yet" description={`Every fact ${me.brand.engine} reads is listed here with its source.`} />
          )
        ) : null}
        {canvasTab === "trace" ? (
          <div className="pt-2">
            <TraceView state={state} />
          </div>
        ) : null}
      </CanvasPanel>
    </div>
  );
}
