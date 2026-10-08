import { humanize } from "@/lib/format";
import { statusTone, type Tone } from "@/lib/status";

export function StatusBadge({ status, tone, label }: { status: string | null | undefined; tone?: Tone; label?: string }) {
  const t = tone ?? statusTone(status);
  return (
    <span className={`mx-badge mx-badge--${t}`} data-status={status ?? undefined}>
      {label ?? humanize(status)}
    </span>
  );
}

/** Amber chip marking a record created/changed by an external system (e.g. Keelson). */
export function ExternalChip({ label = "External", title }: { label?: string; title?: string }) {
  return (
    <span className="mx-badge mx-badge--external" title={title}>
      {label}
    </span>
  );
}
