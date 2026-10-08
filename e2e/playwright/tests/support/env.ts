import path from "node:path";

/** Public URLs (Caddy). Inside the e2e container they resolve to Caddy on `keelson_edge`. */
export const KEELSON = (process.env.KEELSON_URL ?? "http://keelson.localhost").replace(/\/$/, "");
export const MERXA = (process.env.MERXA_URL ?? "http://merxa.localhost").replace(/\/$/, "");
export const AUTH = (process.env.AUTH_URL ?? "http://auth.localhost").replace(/\/$/, "");

/** Where traces, screenshots and reports go (gitignored). */
export const ARTIFACTS = path.resolve(__dirname, "..", "..", "artifacts");
export const SCREENS = path.join(ARTIFACTS, "screens");
export const TRACES = path.join(ARTIFACTS, "traces");

/** The M1 story message (same text as `e2e/api_thin_slice.py`). */
export const TRIM_DELAY_PROMPT =
  "Our trim supplier Meghna Trims says wadding and zips for the Bangladesh POs are delayed 7 days. " +
  "What is the impact and what should we do?";

/**
 * Seeded demo-persona password (`DEMO_USER_PASSWORD` from `.env`, passed in by `run.sh`). Never
 * logged; typed only inside `Tracer.secret()` so it is not recorded in a trace.
 */
export function demoPassword(): string {
  const value = process.env.DEMO_USER_PASSWORD;
  if (!value) throw new Error("DEMO_USER_PASSWORD is not set (run the suite through e2e/playwright/run.sh)");
  return value;
}

/** Real LLM runs take 60–120 s; allow for a slow model. */
export const RUN_TIMEOUT_MS = Number(process.env.E2E_RUN_TIMEOUT_MS ?? 300_000);
