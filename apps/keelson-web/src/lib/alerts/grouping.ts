/**
 * "Why these reports are grouped" (M8 clustering, `docs/contracts/123` §5): presentation helpers for
 * the alert drawer. Every fact comes from the hub's memberships (method, integers, place codes) and
 * its server-built explanation sentence; headlines are untrusted text and stay plain text.
 */
import type { BadgeTone } from "@keelson/ui";
import { attribution } from "./format";
import type { AlertDetailView, AlertMembership, AlertSignal } from "./types";

/** The method as a short chip label. */
export const MEMBERSHIP_LABELS: Record<AlertMembership["method"], string> = {
  anchor: "Official report",
  same_hazard: "Same hazard",
  founded: "First report",
  attach_geo: "Nearby",
  attach_ref: "Same place",
  attach_vector: "Similar description",
  near_duplicate: "Near-duplicate",
  absorbed: "Joined the official event",
  merged_vector: "Merged",
};

export const MEMBERSHIP_TONES: Record<AlertMembership["method"], BadgeTone> = {
  anchor: "info",
  same_hazard: "info",
  founded: "neutral",
  attach_geo: "neutral",
  attach_ref: "neutral",
  attach_vector: "accent",
  near_duplicate: "warning",
  absorbed: "neutral",
  merged_vector: "accent",
};

const MAX_REF_CHIPS = 3;

/** Whole percent, half-up, of a basis-point similarity ("80 %"). */
export function similarityPct(bp: number): string {
  return `${Math.floor((bp + 50) / 100)} %`;
}

/** Evidence chips: distance, shared place codes, similarity, differing fingerprint bits, original method. */
export function membershipChips(m: AlertMembership): string[] {
  const chips: string[] = [];
  if (m.distance_km !== null) chips.push(`${new Intl.NumberFormat("en-GB").format(m.distance_km)} km`);
  const refs = m.shared_refs.slice(0, MAX_REF_CHIPS);
  chips.push(...refs);
  if (m.shared_refs.length > refs.length) chips.push(`+${m.shared_refs.length - refs.length} more`);
  if (m.similarity_bp !== null) chips.push(`similarity ${similarityPct(m.similarity_bp)}`);
  if (m.hamming_bits !== null) chips.push(`${m.hamming_bits} of 64 bits differ`);
  if (m.prior_method !== null) chips.push(`first: ${MEMBERSHIP_LABELS[m.prior_method].toLowerCase()}`);
  return chips;
}

export type GroupedCopy = { signalId: string; source: string; headline: string; bits: number | null };

export type GroupingRow = {
  key: string;
  signalId: string;
  method: AlertMembership["method"];
  label: string;
  tone: BadgeTone;
  chips: string[];
  /** The hub's sentence (built from codes and curated names only). */
  explanation: string;
  /** Untrusted reported text: render as plain text only. */
  headline: string | null;
  source: string | null;
  decidedAt: string;
  /** Syndicated copies of this report (near-duplicates), counted once for corroboration. */
  copies: GroupedCopy[];
};

const RANK: Partial<Record<AlertMembership["method"], number>> = { anchor: 0, founded: 1 };

function isCopy(m: AlertMembership): boolean {
  return m.duplicate_of_signal_id !== null && (m.method === "near_duplicate" || m.prior_method === "near_duplicate");
}

/**
 * One row per current member report, the founding report first, then by decision time; a
 * near-duplicate is listed under its original ("also reported by") when the original is shown.
 * Superseded versions of a report (an earlier GDACS advisory) are left out.
 */
export function groupingRows(detail: Pick<AlertDetailView, "signals" | "memberships">): GroupingRow[] {
  const signals = new Map<string, AlertSignal>(detail.signals.map((s) => [s.id, s]));
  const superseded = new Set(detail.signals.flatMap((s) => (s.supersedes ? [s.supersedes] : [])));
  const current = (detail.memberships ?? []).filter((m) => signals.has(m.signal_id) && !superseded.has(m.signal_id));
  const shown = new Set(current.filter((m) => !isCopy(m)).map((m) => m.signal_id));
  const rows = new Map<string, GroupingRow>();
  const ordered = [...current].sort(
    (a, b) => (RANK[a.method] ?? 2) - (RANK[b.method] ?? 2) || a.decided_at.localeCompare(b.decided_at) || a.signal_id.localeCompare(b.signal_id),
  );
  const copies: AlertMembership[] = [];
  for (const m of ordered) {
    if (isCopy(m) && m.duplicate_of_signal_id !== null && shown.has(m.duplicate_of_signal_id)) {
      copies.push(m);
      continue;
    }
    const s = signals.get(m.signal_id);
    rows.set(m.signal_id, {
      key: m.signal_id,
      signalId: m.signal_id,
      method: m.method,
      label: MEMBERSHIP_LABELS[m.method],
      tone: MEMBERSHIP_TONES[m.method],
      chips: membershipChips(m),
      explanation: m.explanation,
      headline: s ? s.title : null,
      source: s ? attribution(s.source) : null,
      decidedAt: m.decided_at,
      copies: [],
    });
  }
  for (const m of copies) {
    const original = rows.get(m.duplicate_of_signal_id ?? "");
    const s = signals.get(m.signal_id);
    if (original && s) original.copies.push({ signalId: m.signal_id, source: attribution(s.source), headline: s.title, bits: m.hamming_bits });
  }
  return [...rows.values()];
}
