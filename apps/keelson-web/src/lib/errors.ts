/**
 * User-visible failure text from the case stream (`RUN_ERROR`, `SUBAGENT_ERROR`).
 *
 * The engine sends a fixed, generic message ending in "… error ID <uuid>" plus `metadata.errorId`
 * (`services/engine` `public_errors.py`); exception text never reaches the browser. This module is
 * the client-side belt and braces: anything that still looks like a stack trace, an exception
 * dump, a URL or a file path is replaced by a generic sentence before it is stored or shown.
 */

export const GENERIC_RUN_ERROR = "Something went wrong while working on this case.";
export const GENERIC_STEP_ERROR = "This step could not be completed.";

const MAX_MESSAGE_LENGTH = 400;

const UNSAFE_PATTERNS: readonly RegExp[] = [
  /[\r\n]/, // multi-line: tracebacks, dumps
  /Traceback \(most recent call last\)/i,
  /\bFile "[^"]*", line \d+/,
  /(^|\s)at \S+ \(?\S+:\d+:\d+\)?/, // JS stack frame
  /\b[A-Z][A-Za-z0-9_]*(Error|Exception|Warning)(\([^)]*\))?:/, // "ProgrammingError: …"
  /\b[a-z_][a-z0-9_]*\.(py|ts|js|mjs|tsx):\d*/i,
  /\b(https?|postgres(ql)?|redis|valkey):\/\//i,
  /\b(SELECT|INSERT|UPDATE|DELETE)\b[\s\S]*\b(FROM|INTO|SET|WHERE)\b/,
  /\bsqlalchemy|psycopg|asyncpg\b/i,
];

const ERROR_ID_IN_TEXT = /\berror ID ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/i;

/** `message` when it reads like the engine's public text, else `fallback`. */
export function publicErrorMessage(message: unknown, fallback: string = GENERIC_RUN_ERROR): string {
  if (typeof message !== "string") return fallback;
  const text = message.trim();
  if (!text || text.length > MAX_MESSAGE_LENGTH) return fallback;
  return UNSAFE_PATTERNS.some((re) => re.test(text)) ? fallback : text;
}

/** The error reference: `metadata.errorId` when present, else the "error ID <uuid>" in the message. */
export function publicErrorId(metadataErrorId: string | null | undefined, message: unknown): string | null {
  if (metadataErrorId) return metadataErrorId;
  if (typeof message !== "string") return null;
  return ERROR_ID_IN_TEXT.exec(message)?.[1] ?? null;
}
