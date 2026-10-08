/** Formatting and error wording for the back-office. */
import { formatDateTime } from "@keelson/viz";
import { describeError, isApiError } from "../api/client";

const eurFmt = new Intl.NumberFormat("en-GB", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const intFmt = new Intl.NumberFormat("en-GB");

/** Euro amounts from LiteLLM are decimals ("€12.34"). */
export function eur(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? eurFmt.format(value) : "—";
}

export function int(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? intFmt.format(value) : "—";
}

export function when(iso: string | null | undefined): string {
  return iso ? formatDateTime(iso) : "—";
}

/** "30 Sep" for chart axes (UTC, like every other date in the app). */
export function shortDay(isoDate: string): string {
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(isoDate) ? `${isoDate}T00:00:00Z` : isoDate);
  if (Number.isNaN(d.getTime())) return isoDate;
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }).format(d);
}

/** A person-facing sentence for a failed back-office call. */
export function adminError(err: unknown): string {
  if (isApiError(err)) {
    switch (err.code) {
      case "guarded_change":
        // The engine says what was guarded (a limit, the quorum, a first-party connector, …).
        return err.message.trim() ? describeError(err) : "This change needs a platform admin.";
      case "platform_admin_required":
        return "Only a platform admin can do this.";
      case "stale_version":
      case "version_conflict":
      case "conflict":
        return "Someone saved a newer version meanwhile. Reload to see it, then make your change again.";
      default:
        if (err.status === 409 && !err.message) return "That conflicts with the current state. Reload and try again.";
        if (err.status === 404 && err.code === "http_404") return "This part of the back-office isn't available on the server yet.";
    }
  }
  return describeError(err);
}

/** "sk-abcd…" for a key prefix, whether or not the server already appended the ellipsis. */
export function keyPrefix(prefix: string | null | undefined): string {
  const p = (prefix ?? "").trim();
  if (!p) return "—";
  return p.endsWith("…") || p.endsWith("...") ? p : `${p}…`;
}

/** Copy the server's `error_id` (if any) for support. */
export function errorId(err: unknown): string | null {
  if (!isApiError(err)) return null;
  const id = err.body["error_id"];
  return typeof id === "string" ? id : null;
}
