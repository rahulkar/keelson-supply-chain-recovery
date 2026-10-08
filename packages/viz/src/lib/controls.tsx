"use client";

import { Button, Chip } from "@keelson/ui";
import { Info, Table2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { formatValue } from "../format";

export type VisualView = "visual" | "table";

/** "View as table" ⇄ "View as <visual>" (the table always carries the same data as the visual). */
export function ViewToggle({
  view,
  onChange,
  visualLabel,
  visualIcon,
  controls,
}: {
  view: VisualView;
  onChange: (v: VisualView) => void;
  visualLabel: string;
  visualIcon: ReactNode;
  /** id of the region the toggle switches (aria-controls). */
  controls?: string | undefined;
}) {
  return (
    <Button
      size="sm"
      variant="secondary"
      icon={view === "visual" ? <Table2 aria-hidden="true" /> : visualIcon}
      aria-controls={controls}
      onClick={() => onChange(view === "visual" ? "table" : "visual")}
    >
      {view === "visual" ? "View as table" : `View as ${visualLabel}`}
    </Button>
  );
}

export type ChoiceChip = {
  id: string;
  label: string;
  count?: number | undefined;
  title?: string | undefined;
  /** Not selectable (say why in `title`). */
  disabled?: boolean | undefined;
};

/** Single-choice chip group (pressed state on the chosen chip). */
export function ChoiceChips({
  label,
  choices,
  value,
  onChange,
}: {
  label: string;
  choices: readonly ChoiceChip[];
  value: string;
  onChange: (id: string) => void;
}) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-1.5">
      {choices.map((c) => (
        <Chip key={c.id} selected={value === c.id} onClick={() => onChange(c.id)} title={c.title} disabled={c.disabled}>
          {c.label}
          {c.count !== undefined ? ` (${formatValue(c.count, "count")})` : ""}
        </Chip>
      ))}
    </div>
  );
}

/** Calm inline notice (why the visual is not drawn, base map missing, …). */
export function Notice({ children }: { children: ReactNode }) {
  return (
    <p className="flex items-start gap-2 rounded-md bg-surface-2 px-3 py-2 text-caption text-fg-muted">
      <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <span>{children}</span>
    </p>
  );
}

/** Rows shown per page in "View as table" for large payloads. */
export const TABLE_PAGE_SIZE = 100;

/** First `pageSize` rows, then "Show N more" (keeps very large tables responsive). */
export function usePaged<T>(rows: readonly T[], pageSize = TABLE_PAGE_SIZE): { visible: readonly T[]; more: ReactNode } {
  const [limit, setLimit] = useState(pageSize);
  const visible = rows.length > limit ? rows.slice(0, limit) : rows;
  const rest = rows.length - visible.length;
  const more =
    rest > 0 ? (
      <Button size="sm" variant="ghost" onClick={() => setLimit((l) => l + pageSize)}>
        Show {formatValue(Math.min(rest, pageSize), "count")} more (of {formatValue(rows.length, "count")})
      </Button>
    ) : null;
  return { visible, more };
}
