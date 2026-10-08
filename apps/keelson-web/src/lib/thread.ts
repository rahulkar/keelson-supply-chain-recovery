/**
 * Thread layout (`65-web-internals.md`): one turn per run —
 *   user bubble → thinking timeline (collapsed) → assistant narrative → inline blocks → follow-ups.
 *
 * Runs without a user message (resumes after an approach / approval decision, actions) are
 * continuation turns. The narrative is the composed assistant text (TEXT_MESSAGE_*); a
 * `narrative@1` block is used only when a turn has no text (never both, they carry the same text).
 */
import { narrativePropsSchema, planDiffPropsSchema, type NarrativeProps, type PlanDiffProps, type ReplanBanner } from "@keelson/contracts";
import type { PlanVersionView } from "@keelson/viz";
import type { CaseMessage } from "./api/types";
import { blocksOf, type BlockEntry, type CaseStreamState, type RunEntry } from "./stream/reducer";

export type ThreadMessage = Pick<CaseMessage, "id" | "role" | "text" | "run_id" | "created_at">;

export type Turn = {
  key: string;
  runId: string | null;
  user: ThreadMessage | null;
  run: RunEntry | null;
  /** Markdown of the assistant's answer for this turn (may be empty). */
  narrative: string;
  /** A follow-up's one-line "What changed" against the previous plan (M4 `narrative@1` `what_changed`). */
  whatChanged: string | null;
  /** M5 re-plan banner of the turn's narrative ("New information: … — plan updated (v2)"). */
  banner: ReplanBanner | null;
  /**
   * M6 `alert_origin@1` ("Opened from alert"): shown at the top of the turn in place of a user
   * message (a `signal` run has none). Never repeated in `inline` or the canvas.
   */
  origin?: BlockEntry | null | undefined;
  /** `placement: inline` blocks (narrative and alert origin excluded), in emission order. */
  inline: BlockEntry[];
  /** Queued, but a later run already started: this message was never picked up. */
  stale: boolean;
};

function time(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/**
 * Runs still "queued" although another run started after their message was posted. Runs of a
 * case are executed one at a time in order, so such a run will never start (e.g. it failed
 * before RUN_STARTED); it must not keep the case looking busy.
 */
export function staleQueuedRuns(messages: readonly ThreadMessage[], state: CaseStreamState): Set<string> {
  const starts = Object.values(state.runs)
    .map((r) => r.startedAt)
    .filter((t): t is number => t !== null);
  const latestStart = starts.length ? Math.max(...starts) : null;
  const stale = new Set<string>();
  if (latestStart === null) return stale;
  for (const m of messages) {
    if (m.role !== "user" || !m.run_id) continue;
    const run = state.runs[m.run_id];
    const posted = time(m.created_at);
    if (run?.status === "queued" && posted !== null && latestStart > posted) stale.add(m.run_id);
  }
  return stale;
}

/** Running or queued (and not stale) runs exist: block actions wait. */
export function hasActiveWork(state: CaseStreamState, stale: ReadonlySet<string>): boolean {
  return state.runOrder.some((id) => {
    const status = state.runs[id]?.status;
    return status === "running" || (status === "queued" && !stale.has(id));
  });
}

/** The M6 "Opened from alert" card (thread header of a case opened from an alert). */
export function isAlertOrigin(b: BlockEntry): boolean {
  return b.envelope.kind === "alert_origin";
}

function isNarrative(b: BlockEntry): boolean {
  return b.envelope.kind === "narrative";
}

function narrativeMarkdown(b: BlockEntry): string {
  const props = b.envelope.props as { markdown?: unknown } | null;
  return props && typeof props.markdown === "string" ? props.markdown : "";
}

/** The latest `what_changed` line among the turn's narrative blocks (plain text, one line). */
export function whatChangedOf(blocks: readonly BlockEntry[]): string | null {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i]!;
    if (!isNarrative(b)) continue;
    const props = b.envelope.props as { what_changed?: unknown } | null;
    const text = props && typeof props.what_changed === "string" ? props.what_changed.replace(/\s+/g, " ").trim() : "";
    if (text) return text;
  }
  return null;
}

/** Validated `narrative@1` props of a block (`null` for other kinds or invalid props). */
function narrativeProps(b: BlockEntry): NarrativeProps | null {
  if (!isNarrative(b) || b.envelope.v !== 1) return null;
  const parsed = narrativePropsSchema.safeParse(b.envelope.props);
  return parsed.success ? parsed.data : null;
}

/** The latest re-plan banner among the given blocks (M5 `narrative@1.banner`). */
export function bannerOf(blocks: readonly BlockEntry[]): ReplanBanner | null {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const banner = narrativeProps(blocks[i]!)?.banner;
    if (banner) return banner;
  }
  return null;
}

/** The case's newest re-plan banner (for the case header and the live toast). */
export function latestBanner(state: CaseStreamState): ReplanBanner | null {
  return bannerOf(blocksOf(state, state.blockOrder));
}

/**
 * Plan versions for the switcher (v1 ↔ v2): the versions the newest `narrative@1.plan_versions`
 * lists (status, current), else every version a `plan_diff` / `recommended_option` was emitted
 * for. Each carries its latest plan diff and the "What changed" line of the narrative whose
 * banner announced it (or that mentions the version in its `what_changed`, "Plan v2: …").
 */
export function planVersionViews(state: CaseStreamState): PlanVersionView[] {
  const blocks = blocksOf(state, state.blockOrder);
  const diffs = new Map<number, PlanDiffProps>();
  const seen = new Set<number>();
  const whatChanged = new Map<number, string>();
  let links: NarrativeProps["plan_versions"] = [];
  for (const b of blocks) {
    if (b.envelope.kind === "plan_diff" && b.envelope.v === 1) {
      const parsed = planDiffPropsSchema.safeParse(b.envelope.props);
      if (parsed.success) {
        diffs.set(parsed.data.plan_version, parsed.data);
        seen.add(parsed.data.plan_version);
      }
    } else if (b.envelope.kind === "recommended_option") {
      const pv = (b.envelope.props as { plan_version?: unknown } | null)?.plan_version;
      if (typeof pv === "number" && Number.isInteger(pv) && pv >= 1) seen.add(pv);
    }
    const n = narrativeProps(b);
    if (n) {
      if (n.plan_versions.length > 0) links = n.plan_versions;
      const text = n.what_changed?.replace(/\s+/g, " ").trim();
      const version = n.banner?.plan_version ?? (text ? Number(/^Plan v(\d+)\b/i.exec(text)?.[1]) : NaN);
      if (text && Number.isInteger(version)) whatChanged.set(version, text);
    }
  }
  const latest = Math.max(0, ...seen, ...links.map((l) => l.version));
  const rows: PlanVersionView[] =
    links.length > 0
      ? links.map((l) => ({ version: l.version, label: l.label, status: l.status, current: l.current }))
      : [...seen]
          .sort((a, b) => a - b)
          .map((v) => ({ version: v, label: `Plan v${v}`, status: v === latest ? "proposed" : "superseded", current: v === latest }));
  return rows.map((r) => ({ ...r, diff: diffs.get(r.version) ?? null, whatChanged: whatChanged.get(r.version) ?? null }));
}

export function buildTurns(messages: readonly ThreadMessage[], state: CaseStreamState): Turn[] {
  const stale = staleQueuedRuns(messages, state);
  const usersByRun = new Map<string, ThreadMessage[]>();
  const assistantByRun = new Map<string, string[]>();
  const orphans: ThreadMessage[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      if (m.run_id) usersByRun.set(m.run_id, [...(usersByRun.get(m.run_id) ?? []), m]);
      else orphans.push(m);
    } else if (m.role === "assistant" && m.run_id && m.text.trim()) {
      assistantByRun.set(m.run_id, [...(assistantByRun.get(m.run_id) ?? []), m.text]);
    }
  }

  const turns: Turn[] = [];
  const seenRuns = new Set<string>();
  const turnFor = (runId: string, run: RunEntry | null): Turn => {
    const users = usersByRun.get(runId) ?? [];
    const blocks = run ? blocksOf(state, run.blockIds) : [];
    const streamed = run
      ? run.messageIds
          .map((id) => state.messages[id]?.text ?? "")
          .filter((t) => t.trim())
          .join("\n\n")
      : "";
    const stored = (assistantByRun.get(runId) ?? []).join("\n\n");
    const fromBlock = blocks.filter(isNarrative).map(narrativeMarkdown).filter(Boolean).join("\n\n");
    return {
      key: `run:${runId}`,
      runId,
      user: users[0] ?? null,
      run,
      narrative: streamed || stored || fromBlock,
      whatChanged: whatChangedOf(blocks),
      banner: bannerOf(blocks),
      origin: blocks.filter(isAlertOrigin).at(-1) ?? null,
      inline: blocks.filter((b) => b.envelope.placement === "inline" && !isNarrative(b) && !isAlertOrigin(b)),
      stale: stale.has(runId),
    };
  };

  // Started runs in start order, then runs that have not started yet (in the order posted).
  const started = state.runOrder.filter((id) => state.runs[id]?.status !== "queued");
  const waiting = state.runOrder.filter((id) => state.runs[id]?.status === "queued");
  for (const runId of [...started, ...waiting]) {
    seenRuns.add(runId);
    turns.push(turnFor(runId, state.runs[runId] ?? null));
  }
  // User messages whose run the stream has not reported yet (just posted / still queued).
  for (const m of messages) {
    if (m.role !== "user" || !m.run_id || seenRuns.has(m.run_id)) continue;
    seenRuns.add(m.run_id);
    turns.push(turnFor(m.run_id, null));
  }
  for (const m of orphans) {
    turns.push({ key: `msg:${m.id}`, runId: null, user: m, run: null, narrative: "", whatChanged: null, banner: null, inline: [], stale: false });
  }
  // Extra user messages that share a run id (should not happen) still show up.
  for (const [runId, users] of usersByRun) {
    for (const extra of users.slice(1)) {
      turns.push({ key: `msg:${extra.id}`, runId, user: extra, run: null, narrative: "", whatChanged: null, banner: null, inline: [], stale: false });
    }
  }
  return turns;
}

/**
 * Canvas "Result" tab order (80 §6): the option comparison first, then map, impact graph,
 * stock-out timeline and the plan diff; other kinds follow in arrival order.
 */
export const RESULT_TAB_ORDER: readonly string[] = ["scenario_compare", "network_map", "impact_graph", "stockout_timeline", "plan_diff"];

function resultRank(kind: string): number {
  const i = RESULT_TAB_ORDER.indexOf(kind);
  return i === -1 ? RESULT_TAB_ORDER.length : i;
}

/** Canvas blocks (latest snapshot of each), split for the Result and Evidence tabs. */
export function canvasBlocks(state: CaseStreamState): { result: BlockEntry[]; evidence: BlockEntry[] } {
  const canvas = blocksOf(state, state.blockOrder).filter((b) => b.envelope.placement === "canvas" && !isAlertOrigin(b));
  return {
    // Stable sort: same-kind blocks (e.g. two plan diffs) keep their arrival order.
    result: canvas
      .filter((b) => b.envelope.kind !== "evidence_pack")
      .map((b, i) => ({ b, i }))
      .sort((x, y) => resultRank(x.b.envelope.kind) - resultRank(y.b.envelope.kind) || x.i - y.i)
      .map(({ b }) => b),
    evidence: canvas.filter((b) => b.envelope.kind === "evidence_pack"),
  };
}

/** Longest option title quoted in a follow-up chip. */
const CHIP_TITLE_MAX = 48;

function chipTitle(title: string): string {
  const t = title.replace(/\s+/g, " ").trim();
  return t.length > CHIP_TITLE_MAX ? `${t.slice(0, CHIP_TITLE_MAX - 1)}…` : t;
}

/** What the case's latest approach card says the user can change (M4 `approach_card` `editable`). */
export type FollowUpContext = { editable: readonly { label: string; kind: string }[] };

/** The editable values of the case's latest approach card (empty when there is none). */
export function followUpContext(state: CaseStreamState): FollowUpContext {
  const cards = blocksOf(state, state.blockOrder).filter((b) => b.envelope.kind === "approach_card");
  const raw = (cards.at(-1)?.envelope.props as { editable?: unknown } | undefined)?.editable;
  const editable = Array.isArray(raw)
    ? raw.filter(
        (e): e is { label: string; kind: string } =>
          !!e && typeof e === "object" && typeof (e as { label?: unknown }).label === "string" && typeof (e as { kind?: unknown }).kind === "string",
      )
    : [];
  return { editable };
}

/**
 * The re-simulation chip, named after the scenario's own time value ("What if the delay
 * doubles?", "What if the duration doubles?") so it fits any event type; generic otherwise.
 */
function resimulateChip(ctx: FollowUpContext | undefined): string {
  const days = ctx?.editable.find((e) => e.kind === "days" && e.label.trim());
  const label = days?.label.replace(/\s*\([^)]*\)/g, "").replace(/\s+/g, " ").trim().toLowerCase();
  return label ? `What if the ${label} doubles?` : "What if this lasts twice as long?";
}

/**
 * Follow-up chips for the end of the latest finished turn, derived from what the turn produced
 * (the engine does not stream chips). They are sent as ordinary chat messages, which the engine's
 * follow-up step classifies (M4 `followup_delta`: explain / re-simulate / re-investigate); they
 * only ever *ask*, never act.
 */
export function followUps(turn: Turn, ctx?: FollowUpContext): string[] {
  const run = turn.run;
  if (!run || run.status !== "done") return [];
  const kinds = new Set(turn.inline.map((b) => b.envelope.kind));
  if (kinds.has("commit_tracker")) {
    return ["What else is at risk before the peak weeks?", "Summarise what changed in Merxa"];
  }
  const option = turn.inline.find((b) => b.envelope.kind === "recommended_option");
  if (option) {
    const props = option.envelope.props as { alternatives?: { title?: unknown }[] } | null;
    const alt = props?.alternatives?.find((a) => typeof a.title === "string" && a.title.trim() && !/^do nothing$/i.test(a.title.trim()));
    return [
      alt ? `Why not ${chipTitle(alt.title as string)}?` : "Why this option over doing nothing?", // explain
      resimulateChip(ctx), // re-simulate
      "Which stores are most exposed?",
    ];
  }
  return ["What's most at risk in the next four weeks?", "Which purchase orders arrive late?"];
}
