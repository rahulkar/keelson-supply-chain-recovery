"use client";

import { Button, Card, CardHeader } from "@keelson/ui";
import { Braces, RotateCw } from "lucide-react";
import { useId } from "react";

export type RawDataCardProps = {
  /** Whatever could not be rendered (envelope, props, …). */
  data: unknown;
  title?: string | undefined;
  /** Block key (`kind@v`) if known. */
  blockKey?: string | undefined;
  reason?: RawDataReason | undefined;
  issues?: string[] | undefined;
  /** Offers "Try again" (e.g. the `data_ref` payload could not be loaded). */
  onRetry?: (() => void) | undefined;
};

export type RawDataReason =
  | "unknown_kind"
  | "invalid_props"
  | "invalid_data"
  | "data_unavailable"
  | "not_an_envelope"
  | "render_error";

const REASONS: Record<NonNullable<RawDataCardProps["reason"]>, string> = {
  unknown_kind: "This kind of card isn't supported by this version of the app yet.",
  invalid_props: "This card's data didn't match what was expected, so it is shown as raw data.",
  invalid_data: "This card's detailed data didn't match what was expected, so it is shown as raw data.",
  data_unavailable: "This card's detailed data couldn't be loaded.",
  not_an_envelope: "This item isn't a recognisable card.",
  render_error: "This card couldn't be displayed.",
};

function safeJson(data: unknown): string {
  try {
    const seen = new WeakSet<object>();
    return JSON.stringify(
      data,
      (_k, v: unknown) => {
        if (typeof v === "bigint") return v.toString();
        if (v && typeof v === "object") {
          if (seen.has(v)) return "[circular]";
          seen.add(v);
        }
        return v;
      },
      2,
    ) ?? String(data);
  } catch {
    return String(data);
  }
}

/** Fallback for unknown or invalid blocks: never breaks the thread, keeps the data inspectable. */
export function RawDataCard({ data, title, blockKey, reason = "render_error", issues = [], onRetry }: RawDataCardProps) {
  const titleId = useId();
  return (
    <Card labelledBy={titleId} padding="sm" className="border-dashed">
      <div data-block-fallback={reason} data-block-key={blockKey} className="flex flex-col gap-2">
        <CardHeader
          title={title || "Raw data"}
          titleId={titleId}
          icon={<Braces />}
          description={REASONS[reason]}
          eyebrow={blockKey}
          actions={
            onRetry ? (
              <Button size="sm" variant="secondary" icon={<RotateCw aria-hidden="true" />} onClick={onRetry}>
                Try again
              </Button>
            ) : undefined
          }
        />
        {issues.length > 0 ? (
          <ul aria-label="Problems" className="list-disc pl-5 text-caption text-fg-muted">
            {issues.slice(0, 5).map((issue, idx) => (
              <li key={idx} className="font-mono">
                {issue}
              </li>
            ))}
            {issues.length > 5 ? <li>and {issues.length - 5} more</li> : null}
          </ul>
        ) : null}
        <details className="rounded-md bg-surface-2 px-3 py-2">
          <summary className="cursor-pointer text-caption font-medium text-fg-muted">Show data</summary>
          {/* Scrollable, so it must be reachable by keyboard (WCAG 2.1.1). */}
          <pre
            tabIndex={0}
            aria-label={`${title || "Raw data"} (JSON)`}
            className="mt-2 max-h-80 overflow-auto rounded-xs text-caption break-words whitespace-pre-wrap text-fg focus-visible:outline-2 focus-visible:outline-focus"
          >
            <code>{safeJson(data)}</code>
          </pre>
        </details>
      </div>
    </Card>
  );
}
