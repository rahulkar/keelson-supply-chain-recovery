/**
 * "Updated" badge in the case list (M5, `docs/contracts/95` §4): a case this viewer opened before
 * whose `updated_at` is newer than their last look (e.g. a webhook re-plan produced plan v2 while
 * they were away). Per browser, in `localStorage` (a per-viewer convenience: blocked storage just
 * means no badge). Cases never opened here get no badge.
 */
const KEY = "keelson-seen:";

export function markCaseSeen(caseId: string, at: Date = new Date()): void {
  try {
    window.localStorage.setItem(KEY + caseId, at.toISOString());
  } catch {
    // storage blocked: no "Updated" badges, nothing else changes
  }
}

export function caseSeenAt(caseId: string): string | null {
  try {
    return window.localStorage.getItem(KEY + caseId);
  } catch {
    return null;
  }
}

/** The case changed after this viewer last looked at it (both instants known and parseable). */
export function isUpdatedSince(updatedAt: string | null | undefined, seenAt: string | null | undefined): boolean {
  if (!updatedAt || !seenAt) return false;
  const u = Date.parse(updatedAt);
  const s = Date.parse(seenAt);
  return Number.isFinite(u) && Number.isFinite(s) && u > s;
}
