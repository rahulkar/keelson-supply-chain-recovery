import type { BlockEnvelope, CompareOption, MapLink, NetworkMapData, RecommendedOptionProps } from "@keelson/contracts";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  BlockRenderer,
  UNCONFIRMED_APPLY_REASON,
  WHAT_IF_APPLY_REASON,
  WhatChanged,
  approachEditDraft,
  approachEditResult,
  confirmAction,
  eventTypeLabel,
  hasTypedEdit,
  listSentence,
  optionActions,
  parseIdList,
  type BlockAction,
} from "../src";
import { compareChartOption, optionTag, pointGroups } from "../src/compare/model";
import { blockFixtures, getFixture } from "../src/fixtures";
import { IMPACT_UNITS_LABEL, delayedUnitsText } from "../src/graph/model";
import { ALL_OPTIONS, buildMapModel, laneLegs } from "../src/map/model";
import { expectNoA11yViolations } from "./a11y";
import networkMapData from "../fixtures/network_map.data.json";

const fixture = (name: string): BlockEnvelope => {
  const f = getFixture(name);
  if (!f) throw new Error(`missing fixture ${name}`);
  return structuredClone(f.envelope);
};

const renderBlock = (envelope: BlockEnvelope, extra: { busy?: boolean } = {}) => {
  const onAction = vi.fn<(a: BlockAction) => Promise<void>>().mockResolvedValue(undefined);
  const utils = render(<BlockRenderer envelope={envelope} onAction={onAction} {...extra} />);
  return { ...utils, onAction };
};

type ApproachProps = {
  editable: { key: string; label: string; kind: "days" | "pct" | "ids"; value_int: number | null; value_ids: string[] | null }[];
  checks: ({ task_id: string; sentence: string } | string)[];
};

describe("M4 fixtures", () => {
  it("are appended after the M3 ones (e2e keeps its order)", () => {
    const names = blockFixtures.map((f) => f.name);
    const at = names.indexOf("approach_card.m4");
    expect(names.slice(at, at + 3)).toEqual(["approach_card.m4", "recommended_option.what_if", "narrative.what_changed"]);
    expect(at).toBeGreaterThan(names.indexOf("invalid.impact_graph.data"));
  });
});

describe("ApproachCard (M4)", () => {
  it("shows the what-if notice, the agents to consult and the checks as sentences", async () => {
    const { container } = renderBlock(fixture("approach_card.m4"));
    expect(screen.getByText("What-if")).toBeInTheDocument();
    expect(container.querySelector("[data-what-if]")).toHaveTextContent(/nothing can be applied until you promote it/);
    expect(screen.getByText("Chokepoint closure")).toBeInTheDocument();
    const agents = screen.getByRole("region", { name: "Who I'll consult" });
    expect(within(agents).getByText("Logistics analyst, Sourcing analyst, Inventory & demand analyst, Playbook analyst and Critic")).toBeInTheDocument();
    // Avatar stack: 4 shown + "+1" (decorative; the names are in the sentence).
    expect(agents.querySelector("[aria-hidden='true']")).toHaveTextContent("+1");
    const checks = within(screen.getByRole("region", { name: "What I'll check" })).getAllByRole("listitem");
    expect(checks).toHaveLength(5);
    expect(checks[0]).toHaveTextContent("Which open purchase orders sail on lanes through Suez in the next 3 weeks");
    expect(screen.getByText("Unconfirmed")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("a card that already is a what-if does not offer 'Treat as what-if' again", () => {
    const env = fixture("approach_card.m4");
    (env.props as { buttons: string[] }).buttons = ["confirm", "edit", "what_if", "cancel"];
    renderBlock(env);
    const group = screen.getByRole("group", { name: "Approach decision" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(["Looks right", "Edit", "Cancel"]);
    // An actual case keeps it.
    (env.props as { mode: string }).mode = "actual";
    renderBlock(env);
    expect(screen.getByRole("button", { name: "Treat as what-if" })).toBeInTheDocument();
  });

  it("M1 cards (plain-string checks, no editable values) keep the free-text Edit", async () => {
    const user = userEvent.setup();
    renderBlock(fixture("approach_card"));
    expect(screen.queryByRole("region", { name: "Who I'll consult" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("What should I change?")).toHaveFocus();
  });

  it("Edit sends typed edits (only changed values) and removed checks", async () => {
    const user = userEvent.setup();
    const { onAction, container } = renderBlock(fixture("approach_card.m4"));
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit the approach" });
    expect(within(form).getByRole("button", { name: "Send changes" })).toHaveAttribute("aria-disabled", "true");
    await expectNoA11yViolations(container);

    const closure = within(form).getByLabelText("Closure (days)");
    await user.clear(closure);
    await user.type(closure, "14");
    await user.click(within(form).getByRole("button", { name: "Remove PO-VN-0007#2" }));
    await user.type(within(form).getByLabelText("Add to Receipts affected"), "PO-VN-0009#1, PO-VN-0004#1");
    await user.click(within(form).getByRole("button", { name: "Add" }));
    await user.click(within(form).getByRole("checkbox", { name: /freight & expedite policy/ }));
    await user.click(within(form).getByRole("button", { name: "Send changes" }));

    expect(onAction).toHaveBeenCalledWith({
      type: "interrupt_decision",
      interruptId: "approach-case_m4-1",
      decision: "edit",
      payload: {
        edits: [
          { key: "duration_days", value_int: 14 },
          { key: "affected_receipts", value_ids: ["PO-VN-0004#1", "PO-VN-0006#1", "PO-VN-0009#1"] },
        ],
        removed_checks: ["t5"],
      },
    });
  });

  it("refuses invalid values and keeps at least one check", async () => {
    const user = userEvent.setup();
    const { onAction } = renderBlock(fixture("approach_card.m4"));
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit the approach" });
    const delay = within(form).getByLabelText("Extra transit (days)");
    await user.clear(delay);
    await user.type(delay, "-3");
    await user.click(within(form).getByRole("button", { name: "Send changes" }));
    expect(onAction).not.toHaveBeenCalled();
    expect(delay).toHaveAttribute("aria-invalid", "true");
    expect(delay).toHaveAccessibleDescription("Can't be negative.");

    await user.clear(delay);
    await user.type(delay, "12");
    for (const box of within(form).getAllByRole("checkbox")) await user.click(box);
    expect(within(form).getByRole("alert")).toHaveTextContent("Keep at least one check.");
    await user.click(within(form).getByRole("button", { name: "Send changes" }));
    expect(onAction).not.toHaveBeenCalled();
  });

  it("moves focus into the form and back to Edit", async () => {
    const user = userEvent.setup();
    renderBlock(fixture("approach_card.m4"));
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(document.activeElement).toHaveTextContent(/Change any value/);
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();
  });
});

describe("approach edit model", () => {
  const card = (): ApproachProps => ({
    editable: [
      { key: "delay_days", label: "Delay", kind: "days", value_int: 7, value_ids: null },
      { key: "demand_change_pct", label: "Demand", kind: "pct", value_int: 18, value_ids: null },
      { key: "affected_receipts", label: "Receipts", kind: "ids", value_int: null, value_ids: ["A#1", "B#1"] },
    ],
    checks: [{ task_id: "t1", sentence: "One" }, "plain M1 check"],
  });

  it("sends nothing until a value changes; same number typed differently is no change", () => {
    const c = card();
    const draft = approachEditDraft(c);
    expect(approachEditResult(c, draft)).toEqual({ payload: null, changed: false, errors: {} });
    expect(approachEditResult(c, { ...draft, numbers: { ...draft.numbers, delay_days: "007" } }).payload).toBeNull();
  });

  it("pct may go down to -100, days not below 0, whole numbers only", () => {
    const c = card();
    const d = approachEditDraft(c);
    expect(approachEditResult(c, { ...d, numbers: { ...d.numbers, demand_change_pct: "-100" } }).payload).toEqual({
      edits: [{ key: "demand_change_pct", value_int: -100 }],
      removed_checks: [],
    });
    expect(approachEditResult(c, { ...d, numbers: { ...d.numbers, demand_change_pct: "-101" } }).errors).toEqual({ demand_change_pct: "Can't be below -100%." });
    expect(approachEditResult(c, { ...d, numbers: { ...d.numbers, delay_days: "2.5" } }).errors["delay_days"]).toBe("Enter a whole number.");
    expect(approachEditResult(c, { ...d, numbers: { ...d.numbers, delay_days: "" } }).errors["delay_days"]).toBe("Enter a whole number.");
    expect(approachEditResult(c, { ...d, numbers: { ...d.numbers, delay_days: "99999999999999999999" } }).errors["delay_days"]).toBe("That number is too large.");
  });

  it("ids: at least one, at most 500; removed checks only task ids, one check must remain", () => {
    const c = card();
    const d = approachEditDraft(c);
    expect(approachEditResult(c, { ...d, ids: { affected_receipts: [] } }).errors).toEqual({ affected_receipts: "Keep at least one." });
    const many = Array.from({ length: 501 }, (_, i) => `X#${i}`);
    expect(approachEditResult(c, { ...d, ids: { affected_receipts: many } }).errors).toEqual({ affected_receipts: "At most 500." });
    // Unknown ids are ignored.
    const two = { ...c, checks: [{ task_id: "t1", sentence: "One" }, { task_id: "t2", sentence: "Two" }] };
    expect(approachEditResult(two, { ...d, removedChecks: ["t1", "nope"] }).payload).toEqual({ edits: [], removed_checks: ["t1"] });
    const onlyTask = { ...c, checks: [{ task_id: "t1", sentence: "One" }] };
    expect(approachEditResult(onlyTask, { ...d, removedChecks: ["t1"] }).errors).toEqual({ checks: "Keep at least one check." });
    // Regression: a plain M1 sentence is not a plan task, so removing every task check is refused
    // (as the engine's approach_edit_violations does) even though the sentence remains.
    expect(approachEditResult(c, { ...d, removedChecks: ["t1"] })).toMatchObject({ payload: null, errors: { checks: "Keep at least one check." } });
  });

  it("refuses more than 50 changed values in one edit (ResumePayload.edits max 50)", () => {
    const editable = Array.from({ length: 51 }, (_, i) => ({ key: `k${i}`, label: `K${i}`, kind: "days" as const, value_int: 1, value_ids: null }));
    const c = { editable, checks: [] };
    const d = approachEditDraft(c);
    const numbers = Object.fromEntries(editable.map((e) => [e.key, "2"]));
    expect(approachEditResult(c, { ...d, numbers })).toMatchObject({ payload: null, errors: { form: "Change at most 50 values at once." } });
  });

  it("helpers", () => {
    expect(parseIdList(" A#1, B#1;\nA#1  C ")).toEqual(["A#1", "B#1", "C"]);
    expect(hasTypedEdit({ editable: [], checks: ["x"] })).toBe(false);
    expect(hasTypedEdit({ editable: [], checks: [{ task_id: "t", sentence: "s" }] })).toBe(true);
    expect(eventTypeLabel("LOG.chokepoint_closure")).toBe("Chokepoint closure");
    expect(eventTypeLabel("supplier_delay")).toBe("Supplier delay");
    expect(eventTypeLabel("OTHER")).toBe("OTHER");
    expect(listSentence(["A"])).toBe("A");
    expect(listSentence(["A", "B", "C"])).toBe("A, B and C");
  });
});

describe("RecommendedOption (M4)", () => {
  it("what-if: Apply off with the reason, Promote to actual, values to confirm", async () => {
    const user = userEvent.setup();
    const { onAction, container } = renderBlock(fixture("recommended_option.what_if"));
    expect(screen.getByText("What-if")).toBeInTheDocument();
    const apply = screen.getByRole("button", { name: "Apply changes for me" });
    expect(apply).toHaveAttribute("aria-disabled", "true");
    expect(apply).toHaveAccessibleDescription(`Apply changes for me: ${WHAT_IF_APPLY_REASON}`);

    const box = screen.getByRole("region", { name: "Confirm before applying" });
    expect(within(box).getByText("14 days")).toBeInTheDocument();
    expect(within(box).getByText("· Carrier notice (unverified)")).toBeInTheDocument();
    // The confirm action sits in the box, not in the main button row.
    expect(within(screen.getByRole("group", { name: "Next step" })).queryByRole("button", { name: "Confirm" })).toBeNull();
    await user.click(within(box).getByRole("button", { name: "Confirm" }));
    expect(onAction).toHaveBeenCalledWith({
      type: "case_action",
      action: "confirm_assumptions",
      optionId: "opt-B",
      planVersion: 1,
      assumptionKeys: ["delay_days"],
    });
    await user.click(screen.getByRole("button", { name: "Promote to actual" }));
    expect(onAction).toHaveBeenLastCalledWith({ type: "case_action", action: "promote", optionId: "opt-B", planVersion: 1 });
    await expectNoA11yViolations(container);
  });

  it("optionActions never offers Apply on a what-if or with unconfirmed values, and adds Promote", () => {
    const base: Pick<RecommendedOptionProps, "actions" | "what_if" | "unconfirmed"> = {
      actions: [{ type: "apply", label: "Apply", enabled: true, disabled_reason: null }],
      what_if: true,
      unconfirmed: [],
    };
    expect(optionActions(base)).toEqual([
      { type: "apply", label: "Apply", enabled: false, disabled_reason: WHAT_IF_APPLY_REASON },
      { type: "promote", label: "Promote to actual", enabled: true, disabled_reason: null },
    ]);
    const unconfirmed = { ...base, what_if: false, unconfirmed: [{ key: "k", label: "K", value_text: "1 day", source: "s" }] };
    expect(optionActions(unconfirmed)).toEqual([{ type: "apply", label: "Apply", enabled: false, disabled_reason: UNCONFIRMED_APPLY_REASON }]);
    expect(confirmAction(unconfirmed)).toMatchObject({ type: "confirm_assumptions", label: "Confirm", enabled: true });
    expect(confirmAction({ ...unconfirmed, unconfirmed: [] })).toBeNull();
    expect(optionActions({ ...base, what_if: false })).toEqual(base.actions);
  });
});

describe("Narrative (M4)", () => {
  it("shows the compact What changed line above the text", () => {
    const { container } = renderBlock(fixture("narrative.what_changed"));
    const line = container.querySelector("[data-what-changed]");
    expect(line).toHaveTextContent("What changed: Plan v2: closure 21 → 42 days; units protected 4,300 → 6,900; extra cost €18,600 → €27,400");
  });

  it("WhatChanged renders nothing for empty text and flattens whitespace", () => {
    const { container, rerender } = render(<WhatChanged text={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<WhatChanged text={"  a \n b "} />);
    expect(container).toHaveTextContent("What changed: a b");
  });
});

describe("M3 carry-overs", () => {
  it("impact graph metric is 'Units on delayed receipts'", () => {
    expect(IMPACT_UNITS_LABEL).toBe("Units on delayed receipts");
    expect(delayedUnitsText(3200)).toBe("3,200 units on delayed receipts");
  });

  const opt = (id: string, title: string, lost: number, cost: number, recommended = false): CompareOption =>
    ({
      id,
      title,
      levers: [],
      recommended,
      guided_only: false,
      guided_reason: null,
      pareto: true,
      scores: [],
      win_rate_bp: null,
      scorecard: {
        changes: 0,
        cost_breakdown: { expedite_premium: 0, nearshore_premium: 0, transfer: 0 },
        fill_rate_bp: 9000,
        first_breach_shift_days: 0,
        incremental_cost_cents: cost,
        lost_margin_cents: 0,
        lost_units: lost,
        net_benefit_cents: 0,
        shortage_days: 0,
        tts_gain_days: 0,
        incremental_co2e_kg: null,
        co2e_breakdown: null,
        bands: null,
      },
    }) as CompareOption;

  it("Pareto: coincident options share one label 'B = D' on the recommended marker", () => {
    const options = [opt("opt-A", "Rebalance", 900, 1000), opt("opt-B", "Sea-air", 300, 5000), opt("opt-D", "Hybrid", 300, 5000, true)];
    const groups = pointGroups(options, "incremental_cost_cents", "lost_units");
    expect(groups.map((g) => g.label)).toEqual(["Rebalance", "B = D"]);
    expect(groups[1]!.lead.id).toBe("opt-D");
    const chart = compareChartOption({ options } as never, "light", true) as { series: { data: { label: { show: boolean; formatter: () => string } }[] }[] };
    const labels = chart.series[1]!.data.map((p) => (p.label.show ? p.label.formatter() : null));
    expect(labels).toEqual(["Rebalance", null, "B = D"]);
    // ids without a letter fall back to the titles
    expect(optionTag({ id: "opt_rebalance", title: "Rebalance" })).toBe("Rebalance");
    expect(pointGroups([opt("x1", "One", 1, 1), opt("x2", "Two", 1, 1)], "incremental_cost_cents", "lost_units")[0]!.label).toBe("One = Two");
  });

  it("map: a multi-leg sea–air lane is drawn as given — sea leg to the hub, then the flight as an arc", () => {
    const data = networkMapData as NetworkMapData;
    const model = buildMapModel(data, ALL_OPTIONS);
    const seaAirPaths = model.legs.paths.filter((l) => l.link.kind === "sea_air");
    const seaAirArcs = model.legs.arcs.filter((l) => l.link.kind === "sea_air");
    expect(seaAirPaths).toHaveLength(1);
    expect(seaAirPaths[0]!.path).toHaveLength(4);
    expect(seaAirPaths[0]!.path.at(-1)).toEqual([55.06, 25.01]);
    expect(seaAirArcs).toEqual([expect.objectContaining({ from: [55.06, 25.01], to: [-1.16, 52.26] })]);
    // two-point near-shore lanes stay arcs; inbound lanes are polylines
    expect(model.legs.arcs.filter((l) => l.link.kind === "nearshore")).toHaveLength(2);
    expect(model.legs.paths.filter((l) => l.link.kind === "inbound").length).toBe(data.links.filter((l) => l.kind === "inbound").length);
    // a near-shore lane with waypoints is drawn as its polyline; degenerate paths are skipped
    const road = { ...data.links.at(-1)!, path: [[1, 1], [2, 2], [3, 3]] } as MapLink;
    expect(laneLegs([road])).toEqual({ paths: [{ link: road, path: [[1e-6, 1e-6], [2e-6, 2e-6], [3e-6, 3e-6]] }], arcs: [] });
    expect(laneLegs([{ ...road, path: [[1, 1]] } as MapLink])).toEqual({ paths: [], arcs: [] });
  });
});
