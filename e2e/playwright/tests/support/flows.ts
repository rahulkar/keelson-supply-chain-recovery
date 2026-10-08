import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import { keelsonApi, type KeelsonApi } from "./api";
import { RUN_TIMEOUT_MS, TRIM_DELAY_PROMPT } from "./env";

type Envelope = { id: string; kind: string; props: Record<string, unknown> };
type CaseSnapshot = {
  case: { id: string; status: string; title: string };
  blocks: Envelope[];
  open_interrupts: { id: string; kind: string }[];
  last_event_seq: number;
};

async function snapshot(api: KeelsonApi, caseId: string): Promise<CaseSnapshot> {
  const r = await api.ctx.get(`/api/cases/${caseId}`);
  expect(r.status()).toBe(200);
  return (await r.json()) as CaseSnapshot;
}

async function until<T>(what: string, fn: () => Promise<T | null | undefined>, timeout = RUN_TIMEOUT_MS): Promise<T> {
  const deadline = Date.now() + timeout;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 2_000));
  }
}

function mutating(api: KeelsonApi, idem = false): Record<string, string> {
  return { "X-CSRF-Token": api.csrf, ...(idem ? { "Idempotency-Key": randomUUID() } : {}) };
}

/** Start a case as `username` with `text`; returns its id. */
export async function startCase(api: KeelsonApi, text: string, title?: string): Promise<string> {
  const created = await api.ctx.post("/api/cases", { data: title ? { title } : {}, headers: mutating(api) });
  expect(created.status()).toBeLessThan(300);
  const { id } = (await created.json()) as { id: string };
  const posted = await api.ctx.post(`/api/cases/${id}/messages`, { data: { text }, headers: mutating(api, true) });
  expect(posted.status()).toBe(202);
  return id;
}

/**
 * Drive the M1 story through the API up to the human approval (Priya: trim delay → confirm the
 * approach → apply the recommended option). Returns the case id; the approval interrupt is open.
 */
export async function prepareApprovalCase(): Promise<string> {
  const priya = await keelsonApi("priya");
  try {
    const caseId = await startCase(priya, TRIM_DELAY_PROMPT, "E2E keyboard approval");
    const option = await until("the recommended option", async () => {
      const snap = await snapshot(priya, caseId);
      const approach = snap.open_interrupts.find((i) => i.kind === "approach");
      if (approach) {
        const r = await priya.ctx.post(`/api/interrupts/${approach.id}/decision`, {
          data: { decision: "confirm" },
          headers: mutating(priya),
        });
        expect([202, 409]).toContain(r.status());
      }
      return snap.blocks.filter((b) => b.kind === "recommended_option").at(-1) ?? null;
    });
    let props = option.props as { plan_version: number; option: { id: string }; unconfirmed?: { key: string }[] };
    if (props.unconfirmed?.length) {
      // M4 (90 §5): values the plan relies on must be confirmed before Apply → plan v+1.
      const confirmed = await priya.ctx.post(`/api/cases/${caseId}/actions`, {
        data: {
          type: "confirm_assumptions",
          option_id: props.option.id,
          plan_version: props.plan_version,
          assumption_keys: props.unconfirmed.map((u) => u.key),
        },
        headers: mutating(priya, true),
      });
      expect(confirmed.status(), await confirmed.text()).toBe(202);
      const before = props.plan_version;
      const next = await until("the plan after confirming", async () => {
        const snap = await snapshot(priya, caseId);
        const last = snap.blocks.filter((b) => b.kind === "recommended_option").at(-1);
        return last && (last.props as { plan_version: number }).plan_version > before ? last : null;
      });
      props = next.props as typeof props;
    }
    const applied = await priya.ctx.post(`/api/cases/${caseId}/actions`, {
      data: { type: "apply", option_id: props.option.id, plan_version: props.plan_version },
      headers: mutating(priya, true),
    });
    expect(applied.status(), await applied.text()).toBe(202);
    await until("the approval interrupt", async () => {
      const snap = await snapshot(priya, caseId);
      return snap.open_interrupts.some((i) => i.kind === "approval") ? true : null;
    });
    return caseId;
  } finally {
    await priya.ctx.dispose();
  }
}

/** The newest case of `username` that reached `status` (e.g. "committed"), if any. */
export async function latestCase(username: string, status?: string): Promise<string | null> {
  const api = await keelsonApi(username);
  try {
    const r = await api.ctx.get("/api/cases?limit=50");
    const { items } = (await r.json()) as { items: { id: string; status: string }[] };
    return items.find((c) => !status || c.status === status)?.id ?? null;
  } finally {
    await api.ctx.dispose();
  }
}

/** Wait until no run of the case is active (case snapshot status settles). */
export async function waitForCaseStatus(username: string, caseId: string, statuses: string[]): Promise<void> {
  const api = await keelsonApi(username);
  try {
    await until(`case ${caseId} in ${statuses.join("/")}`, async () => {
      const snap = await snapshot(api, caseId);
      return statuses.includes(snap.case.status) ? true : null;
    });
  } finally {
    await api.ctx.dispose();
  }
}
