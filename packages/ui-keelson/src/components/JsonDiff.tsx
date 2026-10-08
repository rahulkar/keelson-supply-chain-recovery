"use client";

import { useMemo, type ReactNode } from "react";
import { cn } from "../lib/cn";
import { diffJson, formatJsonValue, type DiffEntry } from "../lib/jsonDiff";
import { Badge } from "./Badge";

export type JsonDiffProps = {
  before: unknown;
  after: unknown;
  /** Column names ("v3", "v5"). */
  beforeLabel?: string | undefined;
  afterLabel?: string | undefined;
  /** Names the table. */
  caption: string;
  /** Shown when nothing differs. */
  empty?: ReactNode | undefined;
  /** Paths to leave out (e.g. `version`, `tenant`). */
  ignorePaths?: readonly string[] | undefined;
  className?: string | undefined;
};

const KIND_LABEL = { added: "Added", removed: "Removed", changed: "Changed" } as const;
const KIND_TONE = { added: "success", removed: "danger", changed: "info" } as const;

function ValueCell({ value, tone }: { value: unknown; tone: "before" | "after" }) {
  if (value === undefined) return <span className="text-fg-subtle">—</span>;
  if (typeof value === "string" && value.length > 60) {
    return (
      <span className={cn("block font-mono text-caption break-words whitespace-pre-wrap", tone === "before" ? "text-fg-muted" : "text-fg")}>
        {value}
      </span>
    );
  }
  return (
    <code className={cn("font-mono text-caption break-words", tone === "before" ? "text-fg-muted" : "text-fg")}>{formatJsonValue(value, 400)}</code>
  );
}

function Members({ entry }: { entry: DiffEntry }) {
  if (!entry.added?.length && !entry.removed?.length) return null;
  return (
    <ul className="mt-1 flex flex-wrap gap-1" aria-label="Members changed">
      {entry.added?.map((m) => (
        <li key={`+${String(m)}`}>
          <Badge size="sm" tone="success">
            <span aria-hidden="true">+</span>
            <span className="sr-only">added </span>
            {String(m)}
          </Badge>
        </li>
      ))}
      {entry.removed?.map((m) => (
        <li key={`-${String(m)}`}>
          <Badge size="sm" tone="danger">
            <span aria-hidden="true">−</span>
            <span className="sr-only">removed </span>
            {String(m)}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

/** Table of differences between two JSON documents (path · change · before · after). */
export function JsonDiff({
  before,
  after,
  beforeLabel = "Before",
  afterLabel = "After",
  caption,
  empty = "No differences.",
  ignorePaths = [],
  className,
}: JsonDiffProps) {
  const entries = useMemo(
    () => diffJson(before, after).filter((e) => !ignorePaths.some((p) => e.path === p || e.path.startsWith(`${p}.`) || e.path.startsWith(`${p}[`))),
    [before, after, ignorePaths],
  );
  if (entries.length === 0) {
    return <p className={cn("rounded-md bg-surface-2 px-3 py-4 text-center text-ui text-fg-muted", className)}>{empty}</p>;
  }
  return (
    <div
      role="group"
      aria-label={caption}
      tabIndex={0}
      className={cn(
        "overflow-x-auto rounded-md border border-border focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        className,
      )}
    >
      <table className="w-full border-collapse text-ui">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-border bg-surface-2 text-caption text-fg-muted">
            <th scope="col" className="px-3 py-1.5 text-left font-medium">
              Setting
            </th>
            <th scope="col" className="px-3 py-1.5 text-left font-medium">
              Change
            </th>
            <th scope="col" className="px-3 py-1.5 text-left font-medium">
              {beforeLabel}
            </th>
            <th scope="col" className="px-3 py-1.5 text-left font-medium">
              {afterLabel}
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.path} data-kind={e.kind} className="border-b border-border align-top last:border-b-0">
              <th scope="row" className="px-3 py-1.5 text-left font-mono text-caption font-normal whitespace-nowrap text-fg">
                {e.path}
              </th>
              <td className="px-3 py-1.5">
                <Badge size="sm" tone={KIND_TONE[e.kind]}>
                  {KIND_LABEL[e.kind]}
                </Badge>
              </td>
              <td className="px-3 py-1.5">
                <ValueCell value={e.before} tone="before" />
              </td>
              <td className="px-3 py-1.5">
                <ValueCell value={e.after} tone="after" />
                <Members entry={e} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
