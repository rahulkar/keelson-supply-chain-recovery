import { Fragment, type ReactNode } from "react";

/**
 * Record codes such as `PO-BD-0006`, `TRIM-BD-01` or `DE1-NL1`: upper-case letters / digits joined
 * by hyphens. Browsers may wrap at a hyphen, which splits a code across lines ("PO-BD-" / "0006").
 */
export const CODE_PATTERN = /(?<![\w-])[A-Z0-9]+(?:-[A-Z0-9]+)+(?![\w-])/g;

/** Splits text into plain runs and codes (`{code: true}`), preserving everything else. */
export function splitCodes(text: string): { text: string; code: boolean }[] {
  const out: { text: string; code: boolean }[] = [];
  let last = 0;
  for (const m of text.matchAll(CODE_PATTERN)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ text: text.slice(last, at), code: false });
    out.push({ text: m[0], code: true });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), code: false });
  return out;
}

/** Text whose record codes never break across lines (`white-space: nowrap` on each code). */
export function NoBreakCodes({ text }: { text: string }): ReactNode {
  const parts = splitCodes(text);
  if (!parts.some((p) => p.code)) return text;
  return parts.map((p, i) =>
    p.code ? (
      <span key={i} className="whitespace-nowrap" data-code="">
        {p.text}
      </span>
    ) : (
      <Fragment key={i}>{p.text}</Fragment>
    ),
  );
}
