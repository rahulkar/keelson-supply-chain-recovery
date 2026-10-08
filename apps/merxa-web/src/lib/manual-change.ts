/**
 * Manual changes behind the Guide-mode forms (docs/contracts/95 §2): operator-guide deep
 * links open a pre-filled form; saving it calls merxa-api REST with an `Idempotency-Key`
 * (and `If-Match` for record edits), and merxa-api emits one webhook event per save.
 *
 * Pure helpers (no React) so they can be unit-tested and shared by the form components.
 */
import { ApiError } from "./http";
import { newIdempotencyKey } from "./client-api";
import type { SkuQty } from "./params";

/** A failed attempt whose outcome is unknown to the client (it may have been applied). */
export type PendingAttempt = { path: string; body: string; key: string };

/** Outcome unknown: the same request may be retried with the same key (merxa-api replays). */
export function isRetryable(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  return err.status === 0 || err.status >= 500 || err.code === "idempotency_in_flight" || err.status === 409;
}

/** Reuses the key of an ambiguous earlier attempt of the identical request, else a fresh key. */
export function idempotencyKeyFor(pending: PendingAttempt | null, path: string, body: unknown): string {
  if (pending && pending.path === path && pending.body === JSON.stringify(body)) return pending.key;
  return newIdempotencyKey();
}

/** A whole, non-negative quantity typed by a person (`""`, `1.5`, `-2`, `1e3` → null). */
export function parseQty(text: string, { min = 0 }: { min?: number } = {}): number | null {
  const s = text.trim();
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return Number.isSafeInteger(n) && n >= min && n <= 2_147_483_647 ? n : null;
}

/** Guide links carry the record version the step was planned against (`&version=`). */
export function plannedVersion(value: string | undefined): number | null {
  if (!value || !/^\d+$/.test(value)) return null;
  const n = Number(value);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/** Warning when the record changed since the guide step was planned (null when it did not). */
export function versionNotice(planned: number | null, current: number): string | null {
  if (planned === null || planned === current) return null;
  return (
    `This step was planned against version ${planned}; the record is now at version ${current}. ` +
    "Someone changed it since — check the current values before saving."
  );
}

/** `POST /v1/transfer-orders` body (merxa-api `TransferOrderIn`). */
export function transferOrderBody(from: string, to: string, shipDate: string, lines: readonly SkuQty[]) {
  return { from_dc: from, to_dc: to, ship_date: shipDate, lines: lines.map((l) => ({ sku: l.sku, qty: l.qty })) };
}

/** Why a transfer cannot be submitted yet (null = ready). */
export function transferProblem(
  from: string | undefined,
  to: string | undefined,
  shipDate: string | undefined,
  lines: readonly SkuQty[],
  invalid: readonly string[],
): string | null {
  if (!from || !to) return "Choose the From and To DC.";
  if (from === to) return "From and To DC must differ.";
  if (!shipDate || !/^\d{4}-\d{2}-\d{2}$/.test(shipDate)) return "Choose a ship date.";
  if (invalid.length > 0) return "Fix the unreadable lines first.";
  if (lines.length === 0) return "Add at least one line.";
  if (new Set(lines.map((l) => l.sku)).size !== lines.length) return "Each SKU may appear only once.";
  return null;
}

/** Message for a failed save, with the next step for the person. */
export function saveErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = err.problem.detail || err.problem.title || err.message;
    if (err.status === 412) return "This record was changed by someone else. The page shows the current values now; check them and save again.";
    if (err.status === 428) return "The record version is missing. Reload the page and try again.";
    if (err.status === 404) return `Not found: ${detail}`;
    if (err.status === 403) return `Not allowed: ${detail}`;
    if (err.code === "idempotency_in_flight") return "The previous attempt is still being processed. Try again in a moment.";
    if (err.status === 0 || err.status >= 500) return `Merxa could not confirm the save (${detail}). Saving again is safe — it will not be applied twice.`;
    return detail;
  }
  return err instanceof Error ? err.message : String(err);
}

/** Path segment for a code taken from a URL (codes come from integrations / deep links). */
export function seg(code: string): string {
  return encodeURIComponent(code);
}
