/**
 * M8 Evals UI (`docs/contracts/128` §6): overview (runner status, recordings warning, active-run
 * polling, trends, runs + compare selection, results folder), the new-run and import dialogs, run
 * detail (targets, adversarial matrix), case drill-down tabs and the compare page. Engine answer
 * shapes from `routes_admin_evals.py`; every fixture is checked against the generated schemas.
 */
import {
  evalsCaseResultSchema,
  evalsCompareSchema,
  evalsRunDetailSchema,
  evalsRunSchema,
  evalsStatusSchema,
  evalsTrendsSchema,
} from "@keelson/contracts";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EvalsPage } from "@/components/admin/EvalsPage";
import { EvalCaseDetail } from "@/components/admin/evals/EvalCaseDetail";
import { EvalCompareView } from "@/components/admin/evals/EvalCompare";
import { EvalProgress, caseSteps } from "@/components/admin/evals/EvalProgress";
import { EvalRunDetail } from "@/components/admin/evals/EvalRunDetail";
import { ImportEvalsDialog } from "@/components/admin/evals/ImportEvalsDialog";
import {
  bpText,
  deltaText,
  evalEventsSchema,
  evalFilesSchema,
  evalRunListSchema,
  msText,
  newRunRequest,
  parseResultsFile,
  thresholdText,
  trendSeries,
  usdText,
  usdToMicro,
  type NewRunDraft,
} from "@/lib/admin/evalRuns";
import { expectNoA11yViolations } from "./a11y";
import * as F from "./fixtures/m8-evals";
import { fakeApi, jsonResponse, makeMe, withSession, type Route } from "./session";

const nav = vi.hoisted(() => ({ pathname: "/admin/evals", push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, refresh: vi.fn() }),
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

const session = vi.hoisted(() => ({ requireMe: vi.fn() }));
vi.mock("@/lib/server/session", () => session);

vi.setConfig({ testTimeout: 30_000 });

type Json = Record<string, unknown>;
type Server = {
  status: Json;
  runs: Json;
  detail: Json;
  cases: Record<string, Json>;
  events: (after: number) => Json;
  trends: Json;
  compare: Json;
  files: Json;
  created: Json[];
  imports: Json[];
  answers: Record<string, () => Response>;
};

let server: Server;

function makeServer(): Server {
  return {
    status: F.status(),
    runs: F.runList(),
    detail: F.runDetail(),
    cases: { trim_delay_actual: F.caseResult(), adversarial_kb_doc: F.adversarialResult() },
    events: (after) => F.events(after, "succeeded"),
    trends: F.trends(),
    compare: F.compare(),
    files: F.files(),
    created: [],
    imports: [],
    answers: {},
  };
}

function routes(s: Server): Route {
  return (url, init) => {
    const method = init.method ?? "GET";
    const path = url.split("?")[0]!;
    const q = new URL(url, "http://localhost").searchParams;
    const custom = s.answers[`${method} ${path}`];
    if (custom) return custom();
    if (!path.startsWith("/api/admin/evals")) return undefined;
    const p = path.slice("/api/admin/evals".length);
    let r: RegExpExecArray | null;
    if (method === "GET" && p === "/status") return jsonResponse(200, s.status);
    if (method === "GET" && p === "/runs") return jsonResponse(200, s.runs);
    if (method === "POST" && p === "/runs") {
      const body = JSON.parse(String(init.body)) as Json;
      s.created.push(body);
      return jsonResponse(202, F.activeRun({ status: "queued", mode: body["mode"], progress: { cases_done: 0, cases_total: 2, current_case: null, current_turn: null } }));
    }
    if ((r = /^\/runs\/([^/]+)\/events$/.exec(p))) return jsonResponse(200, s.events(Number(q.get("after") ?? 0)));
    if ((r = /^\/runs\/([^/]+)\/cases\/([^/]+)$/.exec(p))) {
      const res = s.cases[decodeURIComponent(r[2]!)];
      return res ? jsonResponse(200, res) : jsonResponse(404, { code: "not_found", message: "case result not found" });
    }
    if ((r = /^\/runs\/([^/]+)\/cancel$/.exec(p)) && method === "POST") return jsonResponse(202, F.activeRun({ cancel_requested: true }));
    if ((r = /^\/runs\/([^/]+)$/.exec(p))) return jsonResponse(200, s.detail);
    if (method === "POST" && p === "/import") {
      s.imports.push(JSON.parse(String(init.body)) as Json);
      return jsonResponse(s.imports.length > 1 ? 200 : 201, { run_id: F.RUN_A, duplicate: s.imports.length > 1 });
    }
    if (method === "POST" && p === "/import/folder") return jsonResponse(200, { imported: [{ file: "20261008T090000Z.json", run_id: F.RUN_B }], duplicates: 1, errors: [] });
    if (method === "GET" && p === "/files") return jsonResponse(200, s.files);
    if (method === "GET" && p === "/compare") return jsonResponse(200, s.compare);
    if (method === "GET" && p === "/trends") return jsonResponse(200, s.trends);
    return undefined;
  };
}

const AVA = () => makeMe({ sub: "sub-ava", username: "ava", name: "Ava Lindqvist", roles: ["platform-admin"], tenants: ["demo-retail"] });

function renderEvals(ui: ReactNode, me = AVA()) {
  const { api, calls } = fakeApi([routes(server)]);
  const Wrapper = withSession(me, api);
  const utils = render(
    <Wrapper>
      <main>{ui}</main>
    </Wrapper>,
  );
  return { ...utils, calls };
}

const settle = () => act(async () => {});

beforeEach(() => {
  server = makeServer();
  nav.push.mockReset();
  session.requireMe.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

// ───────────────────────────── lib ─────────────────────────────

describe("evals lib", () => {
  it("every fixture matches the generated contract schemas", () => {
    expect(evalsStatusSchema.safeParse(F.status()).success).toBe(true);
    expect(evalsStatusSchema.safeParse(F.offlineStatus()).success).toBe(true);
    for (const r of [F.run(), F.importedRun(), F.liveRun(), F.activeRun()]) expect(evalsRunSchema.safeParse(r).error).toBeUndefined();
    expect(evalRunListSchema.safeParse(F.runList()).success).toBe(true);
    expect(evalsRunDetailSchema.safeParse(F.runDetail()).error).toBeUndefined();
    expect(evalsCaseResultSchema.safeParse(F.caseResult()).error).toBeUndefined();
    expect(evalsCaseResultSchema.safeParse(F.adversarialResult()).error).toBeUndefined();
    expect(evalEventsSchema.safeParse(F.events(0, "running")).success).toBe(true);
    expect(evalsTrendsSchema.safeParse(F.trends()).error).toBeUndefined();
    expect(evalsCompareSchema.safeParse(F.compare()).error).toBeUndefined();
    expect(evalFilesSchema.safeParse(F.files()).success).toBe(true);
  });

  it("formats integer units", () => {
    expect([bpText(9091), bpText(10000), bpText(null)]).toEqual(["90.9 %", "100 %", "—"]);
    expect([msText(650), msText(20970), msText(226210), msText(180000)]).toEqual(["650 ms", "21.0 s", "3 min 46 s", "3 min"]);
    expect([usdText(12300), usdText(5_000_000)]).toEqual(["$0.0123", "$5.00"]);
    expect(thresholdText({ metric: "pass_rate_bp", op: "min", value: 8000 })).toBe("≥ 80 %");
    expect(thresholdText({ metric: "options_max_ms", op: "max", value: 180000 })).toBe("≤ 3 min");
    expect([deltaText("pass_rate_bp", 5000), deltaText("options_max_ms", -2910), deltaText("tokens", 0)]).toEqual(["+50 %", "−2.9 s", "No change"]);
    expect([usdToMicro("2.5"), usdToMicro("$0.000001"), usdToMicro("0"), usdToMicro("1e3"), usdToMicro("1.1234567")]).toEqual([2_500_000, 1, null, null, null]);
  });

  it("new-run request: replay needs the acknowledgement; live the typed confirmation and a budget within caps", () => {
    const caps = { max_tokens: 3_000_000, max_cost_microusd: 5_000_000 };
    const all = ["a", "b"];
    const draft: NewRunDraft = { mode: "replay", caseIds: all, acknowledged: false, confirm: "", maxTokens: "", maxCostUsd: "", note: "" };
    expect(newRunRequest(draft, caps, all).problem).toMatch(/Acknowledge/);
    expect(newRunRequest({ ...draft, caseIds: [] }, caps, all).problem).toMatch(/at least one case/);
    expect(newRunRequest({ ...draft, acknowledged: true }, caps, all).body).toEqual({
      mode: "replay", acknowledge_reset: true, case_ids: null, confirm_live: null, budget: null, note: null,
    });
    const live = { ...draft, acknowledged: true, mode: "record_or_replay" as const, caseIds: ["b"] };
    expect(newRunRequest(live, caps, all).problem).toMatch(/budget/);
    expect(newRunRequest({ ...live, maxTokens: "100000", maxCostUsd: "2.50" }, caps, all).problem).toMatch(/Type RUN LIVE/);
    expect(newRunRequest({ ...live, maxTokens: "4000000", maxCostUsd: "2.50", confirm: "RUN LIVE" }, caps, all).problem).toMatch(/above this installation/);
    expect(newRunRequest({ ...live, maxTokens: "100000", maxCostUsd: "2.50", confirm: "RUN LIVE", note: " try " }, caps, all).body).toEqual({
      mode: "record_or_replay", acknowledge_reset: true, case_ids: ["b"], confirm_live: "RUN LIVE",
      budget: { max_tokens: 100000, max_cost_microusd: 2_500_000 }, note: "try",
    });
  });

  it("results files are checked before upload", () => {
    expect(parseResultsFile("{nope")).toEqual({ error: "The file is not JSON." });
    expect(parseResultsFile("[1]")).toEqual({ error: "The file is not a results document." });
    expect(parseResultsFile(JSON.stringify({ schema: "other", cases: [{}] }))).toMatchObject({ error: expect.stringContaining("schema") });
    expect(parseResultsFile(JSON.stringify({ schema: "keelson-evals/1", cases: [] }))).toEqual({ error: "The file has no cases." });
    expect("doc" in parseResultsFile(JSON.stringify({ schema: "keelson-evals/2", cases: [{ id: "a" }] }))).toBe(true);
  });

  it("trend series: gaps for unmeasured runs, targets only where they apply", () => {
    const t = evalsTrendsSchema.parse(F.trends());
    const options = trendSeries(t, "options_max_ms", "all");
    expect(options.points.map((p) => p.value)).toEqual([223090, null]);
    expect(options.threshold?.label).toBe("≤ 3 min");
    expect(trendSeries(t, "options_max_ms", "replay").threshold).toBeUndefined(); // latency targets are live-only
    expect(trendSeries(t, "pass_rate_bp", "replay").threshold?.value).toBe(8000);
  });

  it("case steps follow the progress events", () => {
    const run = { case_ids: ["trim_delay_actual", "store_shortage_question", "x"] };
    const steps = caseSteps(run, evalEventsSchema.parse(F.events(0, "succeeded")).items);
    expect(steps.map((s) => [s.id, s.state, s.detail])).toEqual([
      ["trim_delay_actual", "failed", "error · replay miss"],
      ["store_shortage_question", "ok", "pass"],
      ["x", "pending", undefined],
    ]);
  });
});

// ───────────────────────────── overview ─────────────────────────────

describe("evals overview", () => {
  it("runner offline: start hint, no-recordings warning, New run disabled", async () => {
    server.status = F.offlineStatus();
    const { container } = renderEvals(<EvalsPage />);
    expect(await screen.findByText("Runner offline")).toBeInTheDocument();
    expect(screen.getByText("docker compose --profile evals up -d evals-runner")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("replay runs will stop at the first model call");
    expect(screen.getByRole("button", { name: "New run" })).toHaveAttribute("aria-disabled", "true");
    await settle();
    await expectNoA11yViolations(container);
  });

  it("trends render accessible tables; the runs table compares two selected runs (older = base)", async () => {
    const user = userEvent.setup();
    const { container } = renderEvals(<EvalsPage />);
    const pass = await screen.findByRole("table", { name: "Pass rate" });
    expect(within(pass).getAllByRole("row").map((r) => r.textContent)).toEqual(["RunValue", "7 Oct, 08:080 %", "8 Oct, 09:0050 %", "Target≥ 80 %"]);
    expect(screen.getByRole("table", { name: "Case results over the last runs" })).toHaveTextContent("adversarial_kb_doc");
    expect(screen.getByText(/Token counts of imported command-line runs are approximate/)).toBeInTheDocument();
    expect(screen.getByText("Runner online")).toBeInTheDocument();
    expect(screen.getByText(/1,240 model answers/)).toBeInTheDocument();

    const compare = screen.getByRole("button", { name: "Compare selected" });
    expect(compare).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("checkbox", { name: /Compare the run of 8 Oct 2026, 09:00/ }));
    await user.click(screen.getByRole("checkbox", { name: /Compare the run of 7 Oct 2026, 08:08/ }));
    await user.click(compare);
    expect(nav.push).toHaveBeenCalledWith(`/admin/evals/compare?base=${F.RUN_A}&head=${F.RUN_B}`);
    await settle();
    await expectNoA11yViolations(container);
  });

  it("results folder: imported files link to their run; the others import one by one or all", async () => {
    const user = userEvent.setup();
    const { calls } = renderEvals(<EvalsPage />);
    const table = await screen.findByRole("table", { name: "Results files" });
    const imported = within(table).getByRole("rowheader", { name: "20261007T080848Z.json" }).closest("tr")!;
    expect(within(imported).getByRole("link", { name: "Open run" })).toHaveAttribute("href", `/admin/evals/runs/${F.RUN_A}`);
    const fresh = within(table).getByRole("rowheader", { name: "20261008T090000Z.json" }).closest("tr")!;
    expect(fresh).toHaveTextContent("1 / 2 passed · 7 checks");
    await user.click(within(fresh).getByRole("button", { name: /Import/ }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/evals/import/folder?file=20261008T090000Z.json" && c.method === "POST")).toBe(true));
    await user.click(screen.getByRole("button", { name: "Import all new" }));
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/admin/evals/import/folder").length).toBe(1));
  });

  it("active run: polls its events every 2 s and stops at the final status", async () => {
    vi.useFakeTimers();
    let polls = 0;
    server.events = (after) => {
      polls += 1;
      return F.events(after, polls >= 2 ? "succeeded" : "running");
    };
    const onChanged = vi.fn();
    renderEvals(<EvalProgress run={evalsRunSchema.parse(F.activeRun()) as never} onChanged={onChanged} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(polls).toBe(1);
    expect(screen.getByRole("meter", { name: "Cases done" })).toHaveAttribute("aria-valuetext", "0 of 2");
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(polls).toBe(2);
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("meter", { name: "Cases done" })).toHaveAttribute("aria-valuetext", "2 of 2");
    expect(screen.queryByRole("button", { name: "Cancel run" })).toBeNull();
    const log = screen.getByRole("list", { name: "Cases" });
    expect(log).toHaveTextContent("replay miss");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(polls).toBe(2); // no more polling once finished
  });

  it("cancel asks first, then posts", async () => {
    const user = userEvent.setup();
    server.events = (after) => F.events(after, "running");
    const { calls } = renderEvals(<EvalProgress run={evalsRunSchema.parse(F.activeRun()) as never} onChanged={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Cancel this eval run?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(calls.some((c) => c.url === `/api/admin/evals/runs/${F.RUN_ACTIVE}/cancel` && c.method === "POST")).toBe(true));
  });
});

// ───────────────────────────── dialogs ─────────────────────────────

describe("new run dialog", () => {
  it("live is disabled with the engine's reason; replay needs the acknowledgement; POST + navigation", async () => {
    const user = userEvent.setup();
    const { container, calls } = renderEvals(<EvalsPage />);
    await user.click(await screen.findByRole("button", { name: "New run" }));
    const dialog = await screen.findByRole("alertdialog", { name: "New eval run" });
    const liveRadio = within(dialog).getByRole("radio", { name: /Live · record or replay/ });
    expect(liveRadio).toBeDisabled();
    expect(liveRadio).toHaveAccessibleDescription(/EVALS_LIVE_ENABLED=0/);
    expect(within(dialog).getByRole("radio", { name: /Replay/ })).toBeChecked();
    expect(within(dialog).getByText(/skipped in replay \(needs live embeddings\)/)).toBeInTheDocument();
    const start = within(dialog).getByRole("button", { name: "Start replay run" });
    expect(start).toHaveAttribute("aria-disabled", "true");
    await user.click(within(dialog).getByRole("checkbox", { name: /Store shortage question/ }));
    await user.click(within(dialog).getByRole("checkbox", { name: /Each case resets Merxa's demo data/ }));
    expect(start).not.toHaveAttribute("aria-disabled", "true");
    await expectNoA11yViolations(dialog);
    await user.click(start);
    await waitFor(() => expect(server.created).toHaveLength(1));
    expect(server.created[0]).toEqual({
      mode: "replay", acknowledge_reset: true,
      case_ids: ["adversarial_kb_doc", "adversarial_supplier_message", "trim_delay_actual"],
      confirm_live: null, budget: null, note: null,
    });
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/admin/evals/runs/${F.RUN_ACTIVE}`));
    expect(calls.find((c) => c.method === "POST")!.headers["X-CSRF-Token"]).toBe("csrf-test");
    expect(container).toBeTruthy();
  });

  it("live: confirm stays disabled until RUN LIVE is typed and the budget is valid", async () => {
    const user = userEvent.setup();
    server.status = F.status({ modes: ["replay", "record_or_replay", "record"].map((mode) => ({ mode, available: true, reason: null, message: null })) });
    renderEvals(<EvalsPage />);
    await user.click(await screen.findByRole("button", { name: "New run" }));
    const dialog = await screen.findByRole("alertdialog", { name: "New eval run" });
    await user.click(within(dialog).getByRole("radio", { name: /Live · record or replay/ }));
    await user.click(within(dialog).getByRole("checkbox", { name: /Each case resets Merxa's demo data/ }));
    const start = within(dialog).getByRole("button", { name: "Start live run" });
    expect(start).toHaveAttribute("aria-disabled", "true");
    // the estimate comes from the last live run's cost per case ($0.15 × 4 cases)
    expect(within(dialog).getByText(/about \$0\.6000 for 4 case/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Max tokens"), "200000");
    await user.type(within(dialog).getByLabelText("Max cost (USD)"), "3");
    expect(start).toHaveAttribute("aria-disabled", "true");
    await user.type(within(dialog).getByLabelText("Type RUN LIVE to confirm"), "run live");
    expect(start).toHaveAttribute("aria-disabled", "true");
    await user.clear(within(dialog).getByLabelText("Type RUN LIVE to confirm"));
    await user.type(within(dialog).getByLabelText("Type RUN LIVE to confirm"), "RUN LIVE");
    expect(start).not.toHaveAttribute("aria-disabled", "true");
    await user.clear(within(dialog).getByLabelText("Max cost (USD)"));
    await user.type(within(dialog).getByLabelText("Max cost (USD)"), "6");
    expect(start).toHaveAttribute("aria-disabled", "true");
    expect(within(dialog).getByText(/above this installation's caps/)).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("Max cost (USD)"));
    await user.type(within(dialog).getByLabelText("Max cost (USD)"), "3");
    await user.click(start);
    await waitFor(() => expect(server.created).toHaveLength(1));
    expect(server.created[0]).toMatchObject({ mode: "record_or_replay", confirm_live: "RUN LIVE", budget: { max_tokens: 200000, max_cost_microusd: 3_000_000 }, case_ids: null });
  });
});

describe("import dialog", () => {
  it("checks the file client-side, then imports; a second import is a duplicate with a link", async () => {
    const user = userEvent.setup();
    renderEvals(<ImportEvalsDialog open onOpenChange={vi.fn()} onImported={vi.fn()} />);
    const dialog = await screen.findByRole("alertdialog", { name: "Import eval results" });
    const input = within(dialog).getByLabelText("Results file");
    await user.upload(input, new File(["{bad"], "x.json", { type: "application/json" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The file is not JSON.");
    expect(within(dialog).getByRole("button", { name: "Import" })).toHaveAttribute("aria-disabled", "true");
    const doc = JSON.stringify({ schema: "keelson-evals/1", started_at: "2026-10-07T08:08:48+00:00", cases: [{ id: "a" }, { id: "b" }] });
    await user.upload(input, new File([doc], "20261007T080848Z.json", { type: "application/json" }));
    expect(await within(dialog).findByText("20261007T080848Z.json: keelson-evals/1 · 2 case(s)")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Import" }));
    expect(await within(dialog).findByRole("status")).toHaveTextContent("Imported.");
    expect(server.imports[0]).toMatchObject({ schema: "keelson-evals/1" });
    await user.upload(input, new File([doc], "copy.json", { type: "application/json" }));
    await user.click(await within(dialog).findByRole("button", { name: "Import" }));
    const status = await within(dialog).findByText(/imported before/);
    expect(within(status.closest("p")!).getByRole("link", { name: "Open the run" })).toHaveAttribute("href", `/admin/evals/runs/${F.RUN_A}`);
  });
});

// ───────────────────────────── run detail & drill-down ─────────────────────────────

describe("run detail", () => {
  it("targets with outcomes, cases linking to the drill-down, the adversarial matrix, export and compare", async () => {
    const { container } = renderEvals(<EvalRunDetail runId={F.RUN_B} />);
    const targets = await screen.findByRole("table", { name: "Targets" });
    const passRow = within(targets).getByRole("rowheader", { name: "Pass rate" }).closest("tr")!;
    expect(passRow).toHaveTextContent("≥ 80 %");
    expect(passRow).toHaveTextContent("50 %");
    expect(passRow).toHaveTextContent("Missed");
    const options = within(targets).getByRole("rowheader", { name: "Options (max)" }).closest("tr")!;
    expect(options).toHaveTextContent("Not for this mode");
    expect(within(targets).getByRole("rowheader", { name: "Numeric grounding (min)" }).closest("tr")).toHaveTextContent("Met");
    expect(screen.getByRole("link", { name: "Trim supplier delay (actual)" })).toHaveAttribute("href", `/admin/evals/runs/${F.RUN_B}/cases/trim_delay_actual`);
    const matrix = await screen.findByRole("table", { name: "Adversarial outcomes" });
    const adv = within(matrix).getByRole("rowheader", { name: "adversarial_kb_doc" }).closest("tr")!;
    expect(within(adv).getAllByText("Held")).toHaveLength(5);
    expect(adv).toHaveTextContent("Failed (soft)");
    expect(adv).toHaveTextContent("Not checked");
    expect(screen.getByRole("link", { name: "Export JSON" })).toHaveAttribute("href", `/api/admin/evals/runs/${F.RUN_B}/export`);
    expect(await screen.findByRole("link", { name: "Compare with previous" })).toHaveAttribute("href", `/admin/evals/compare?base=${F.RUN_LIVE}&head=${F.RUN_B}`);
    await settle();
    await expectNoA11yViolations(container);
  });
});

describe("case drill-down", () => {
  it("tabs: checks (failed first, details as text), trajectory diff, entity chips, latency meters, usage", async () => {
    const user = userEvent.setup();
    const { container } = renderEvals(<EvalCaseDetail runId={F.RUN_B} caseId="trim_delay_actual" />);
    expect(await screen.findByRole("heading", { level: 1, name: "Trim supplier delay (actual)" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open case" })).toHaveAttribute("href", `/cases/${F.KEELSON_CASE}`);
    const checks = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    expect(checks[0]).toMatch(/^report\.affected_entities/);
    expect(checks[1]).toMatch(/^report\.trajectory.*Failed \(soft\)/);
    // model-written text stays text
    expect(screen.getByText(/<b>recall<\/b> 0\.67/)).toBeInTheDocument();
    expect(container.querySelector("b")).toBeNull();
    await expectNoA11yViolations(container);

    await user.click(screen.getByRole("tab", { name: "Trajectory" }));
    const required = screen.getByRole("list", { name: "Required calls of turn report" });
    expect(within(required).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "inventory_demand:*called", "sourcing:*called", "playbook:*missing",
    ]);
    expect(screen.getByText("none called")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Tool calls of turn report" })).toHaveTextContent("list_purchase_orders2");

    await user.click(screen.getByRole("tab", { name: "Entities" }));
    const chips = screen.getByRole("list", { name: "Golden entities of turn report" });
    expect(within(chips).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["UK1 found", "NL1 found", "PO-BD-0001 found", "PO-BD-0002 missing"]);

    await user.click(screen.getByRole("tab", { name: "Latency" }));
    expect(screen.getByRole("meter", { name: "Options (budget 3 min)" })).toHaveAttribute("aria-valuetext", "3 min 46 s of 3 min (over budget)");
    expect(screen.getByRole("meter", { name: "First stage (budget 2.0 s)" })).toHaveAttribute("aria-valuetext", "650 ms of 2.0 s");

    await user.click(screen.getByRole("tab", { name: "Usage" }));
    const usage = screen.getByRole("table", { name: "LLM usage per agent" });
    expect(within(usage).getByRole("rowheader", { name: "planner" }).closest("tr")).toHaveTextContent("$0.0062");
    expect(screen.getByText("Engine-metered calls")).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Adversarial" })).toBeNull();
  });

  it("an adversarial case has the adversarial tab; a missing Keelson case id is no link", async () => {
    const user = userEvent.setup();
    server.cases["adversarial_kb_doc"] = { ...F.adversarialResult(), keelson_case_id: "not-a-uuid" };
    renderEvals(<EvalCaseDetail runId={F.RUN_B} caseId="adversarial_kb_doc" />);
    await user.click(await screen.findByRole("tab", { name: "Adversarial" }));
    expect(screen.getByRole("table", { name: "Adversarial outcomes" })).toHaveTextContent("Failed (soft)");
    expect(screen.queryByRole("link", { name: "Open case" })).toBeNull();
  });
});

describe("compare", () => {
  it("delta tiles toned by direction; regressed rows danger, fixed rows highlighted; check changes", async () => {
    const user = userEvent.setup();
    const { container } = renderEvals(<EvalCompareView base={F.RUN_A} head={F.RUN_B} />);
    const table = await screen.findByRole("table", { name: "Case changes" });
    const regressed = within(table).getByRole("rowheader", { name: /trim_delay_actual/ }).closest("tr")!;
    expect(regressed.className).toContain("danger");
    expect(regressed).toHaveTextContent("Regressed");
    expect(regressed).toHaveTextContent("−25 %");
    const fixed = within(table).getByRole("rowheader", { name: /adversarial_kb_doc/ }).closest("tr")!;
    expect(fixed.className).toContain("accent");
    await user.click(within(fixed).getByText(/0 newly failing · 2 newly passing/));
    expect(within(fixed).getByText(/case\.guard_flagged/)).toBeVisible();
    expect(screen.getByText("+50 % (better)")).toBeInTheDocument(); // pass rate up
    expect(screen.getByText("+2.9 s (worse)")).toBeInTheDocument(); // options slower
    await settle();
    await expectNoA11yViolations(container);
  });

  it("the run pickers navigate", async () => {
    const user = userEvent.setup();
    renderEvals(<EvalCompareView base={F.RUN_A} head={F.RUN_B} />);
    await screen.findByRole("table", { name: "Case changes" });
    await user.selectOptions(screen.getByLabelText("Base (before)"), F.RUN_LIVE);
    expect(nav.push).toHaveBeenCalledWith(`/admin/evals/compare?base=${F.RUN_LIVE}&head=${F.RUN_B}`);
  });
});

describe("routes", () => {
  it("are for platform admins; malformed ids are not found", async () => {
    const { default: RunRoute } = await import("@/app/(app)/admin/evals/runs/[runId]/page");
    const { default: CaseRoute } = await import("@/app/(app)/admin/evals/runs/[runId]/cases/[caseId]/page");
    const { default: CompareRoute } = await import("@/app/(app)/admin/evals/compare/page");
    await expect(RunRoute({ params: Promise.resolve({ runId: "../x" }) })).rejects.toThrow("NEXT_NOT_FOUND");
    await expect(CaseRoute({ params: Promise.resolve({ runId: F.RUN_B, caseId: "a/b" }) })).rejects.toThrow("NEXT_NOT_FOUND");
    await expect(CompareRoute({ searchParams: Promise.resolve({ base: F.RUN_A }) })).rejects.toThrow("NEXT_NOT_FOUND");
    const sam = makeMe({ roles: ["tenant-admin"] });
    session.requireMe.mockResolvedValue(sam);
    const { unmount } = renderEvals(await RunRoute({ params: Promise.resolve({ runId: F.RUN_B }) }), sam);
    expect(screen.getByRole("heading", { level: 1, name: "Platform admins only" })).toBeInTheDocument();
    unmount();
    session.requireMe.mockResolvedValue(AVA());
    renderEvals(await CompareRoute({ searchParams: Promise.resolve({ base: F.RUN_A, head: F.RUN_B }) }));
    expect(await screen.findByRole("heading", { level: 1, name: "Compare eval runs" })).toBeInTheDocument();
  });
});
