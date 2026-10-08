/**
 * Typed **Edit** of an approach card (M4, `docs/contracts/90` §2): the draft the Edit form holds
 * and the decision payload it sends. Pure, so the rules can be tested without rendering.
 *
 * The rules mirror the engine's `blocks.approach_edit_violations` and the `ApproachEdit` /
 * `ResumePayload` limits: only the card's `editable` keys; `days` → whole number ≥ 0, `pct` →
 * whole number ≥ −100, `ids` → 1–500 unique ids; removed checks are task ids on the card and at
 * least one check remains; at most 50 edits / removed checks. Only values that changed are sent.
 */
import { checkTaskId, type ApproachCardProps, type EditableValue } from "@keelson/contracts";
import type { ApproachEditPayload, ApproachEditValue } from "../types";

export const APPROACH_EDIT_LIMITS = {
  /** `ResumePayload.edits` / `removed_checks` (contracts `MAX_PAYLOAD_KEYS`). */
  maxKeys: 50,
  /** `ApproachEdit.value_ids` (contracts `MAX_EDIT_IDS`). */
  maxIds: 500,
  /** Lowest value per integer kind (`approach_edit_violations`). */
  min: { days: 0, pct: -100 } as const,
} as const;

/** What the form holds: number fields as typed text, id lists, the task ids the user unticked. */
export type ApproachEditDraft = {
  numbers: Record<string, string>;
  ids: Record<string, string[]>;
  removedChecks: string[];
};

export type ApproachEditResult = {
  /** The decision payload (only changed values); `null` while there are errors. */
  payload: ApproachEditPayload | null;
  /** Something differs from the card. */
  changed: boolean;
  /** Per editable key, plus `checks` for the check list and `form` for the whole edit. */
  errors: Record<string, string>;
};

/** The form's starting state: the card's current values, every check kept. */
export function approachEditDraft(card: Pick<ApproachCardProps, "editable">): ApproachEditDraft {
  const numbers: Record<string, string> = {};
  const ids: Record<string, string[]> = {};
  for (const e of card.editable) {
    if (e.kind === "ids") ids[e.key] = [...(e.value_ids ?? [])];
    else numbers[e.key] = e.value_int === null || e.value_int === undefined ? "" : String(e.value_int);
  }
  return { numbers, ids, removedChecks: [] };
}

/** "PO-1#1, PO-2#1\nPO-3#2" → unique, trimmed ids in the order typed. */
export function parseIdList(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean))];
}

function sameIds(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

function intError(e: EditableValue, text: string): string | null {
  const t = text.trim();
  if (!t) return "Enter a whole number.";
  if (!/^-?\d+$/.test(t)) return "Enter a whole number.";
  const n = Number(t);
  if (!Number.isSafeInteger(n)) return "That number is too large.";
  const min = e.kind === "pct" ? APPROACH_EDIT_LIMITS.min.pct : APPROACH_EDIT_LIMITS.min.days;
  if (n < min) return e.kind === "pct" ? `Can't be below ${min}%.` : "Can't be negative.";
  return null;
}

/** Validate the draft against the card and build the `edit` decision payload. */
export function approachEditResult(card: Pick<ApproachCardProps, "editable" | "checks">, draft: ApproachEditDraft): ApproachEditResult {
  const errors: Record<string, string> = {};
  const edits: ApproachEditValue[] = [];
  for (const e of card.editable) {
    if (e.kind === "ids") {
      const next = draft.ids[e.key] ?? [];
      const before = e.value_ids ?? [];
      if (sameIds(next, before)) continue;
      if (next.length === 0) errors[e.key] = "Keep at least one.";
      else if (next.length > APPROACH_EDIT_LIMITS.maxIds) errors[e.key] = `At most ${APPROACH_EDIT_LIMITS.maxIds}.`;
      else edits.push({ key: e.key, value_ids: [...next] });
      continue;
    }
    const text = draft.numbers[e.key] ?? "";
    if (text.trim() === (e.value_int === null || e.value_int === undefined ? "" : String(e.value_int))) continue;
    const err = intError(e, text);
    if (err) errors[e.key] = err;
    else if (Number(text.trim()) !== e.value_int) edits.push({ key: e.key, value_int: Number(text.trim()) });
  }

  if (edits.length > APPROACH_EDIT_LIMITS.maxKeys) errors["form"] = `Change at most ${APPROACH_EDIT_LIMITS.maxKeys} values at once.`;

  // Same rule as the engine (`approach_edit_violations`): at least one *plan-task* check must
  // remain — plain M1 sentences on the card do not count (they cannot be kept or removed).
  const taskIds = [...new Set(card.checks.map(checkTaskId).filter((id): id is string => id !== null))];
  const removed = [...new Set(draft.removedChecks)].filter((id) => taskIds.includes(id));
  if (removed.length > 0 && removed.length >= taskIds.length) errors["checks"] = "Keep at least one check.";
  if (removed.length > APPROACH_EDIT_LIMITS.maxKeys) errors["checks"] = `Remove at most ${APPROACH_EDIT_LIMITS.maxKeys} checks at once.`;

  const changed = edits.length > 0 || removed.length > 0 || Object.keys(errors).length > 0;
  const ok = Object.keys(errors).length === 0 && (edits.length > 0 || removed.length > 0);
  return { payload: ok ? { edits, removed_checks: removed } : null, changed, errors };
}

/** The card supports the typed Edit form (M4): editable values, or checks that are plan tasks. */
export function hasTypedEdit(card: Pick<ApproachCardProps, "editable" | "checks">): boolean {
  return card.editable.length > 0 || card.checks.some((c) => checkTaskId(c) !== null);
}
