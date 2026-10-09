import { blockEnvelopeSchema, type BlockEnvelope, type ScenarioCompareProps } from "@keelson/contracts";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BlockRenderer, type BlockAction } from "../src";
import { INTEGER_FALLBACK_TEXT, integerNote } from "../src/compare/integer";
import { ScorecardTable } from "../src/blocks/ScenarioCompare";
import { blockFixtures, getFixture } from "../src/fixtures";
import { expectNoA11yViolations } from "./a11y";

const fixture = (name: string): BlockEnvelope => {
  const f = getFixture(name);
  if (!f) throw new Error(`missing fixture ${name}`);
  return structuredClone(f.envelope);
};

const optionsOf = (envelope: BlockEnvelope) => (envelope.props as ScenarioCompareProps).options;

describe("M8 whole-unit note in the scenario compare (125)", () => {
  it("the fixture is appended after the M7 ones and validates against the envelope contract", () => {
    const names = blockFixtures.map((f) => f.name);
    expect(names.indexOf("scenario_compare.integer")).toBe(names.indexOf("scenario_compare.advisory") + 1);
    expect(blockEnvelopeSchema.safeParse(fixture("scenario_compare.integer")).success).toBe(true);
  });

  it("captions every method in plain language", () => {
    expect(integerNote({ method: "cpsat_optimal", gap_bp: 0, fallback_reason: null })).toBe(
      "Whole units: CP-SAT, proven optimal",
    );
    expect(integerNote({ method: "cpsat_feasible", gap_bp: 40, fallback_reason: null })).toBe(
      "Whole units: CP-SAT, best found (gap 0.4 %)",
    );
    expect(integerNote({ method: "cpsat_feasible", gap_bp: 3, fallback_reason: null })).toBe(
      "Whole units: CP-SAT, best found (gap < 0.1 %)",
    );
    expect(integerNote({ method: "cpsat_feasible", gap_bp: null, fallback_reason: null })).toBe(
      "Whole units: CP-SAT, best found",
    );
    expect(integerNote({ method: "lp_rounding", gap_bp: null, fallback_reason: "time_limit_no_solution" })).toBe(
      "Whole units: rounded from the LP (CP-SAT fallback: time limit)",
    );
    expect(integerNote({ method: "lp_rounding", gap_bp: null, fallback_reason: "no_improvement" })).toBe(
      "Whole units: rounded from the LP (CP-SAT found nothing better)",
    );
    expect(integerNote({ method: "lp_rounding", gap_bp: null, fallback_reason: null })).toBe("Whole units: rounded from the LP");
    expect(integerNote(null)).toBeNull();
    expect(Object.keys(INTEGER_FALLBACK_TEXT)).toHaveLength(11);
  });

  it("the scorecard shows the note only on options that carry one, and stays axe-clean", async () => {
    const options = optionsOf(fixture("scenario_compare.integer"));
    const withFallback = options.map((o) =>
      o.id === "opt_hybrid" ? { ...o, integer: { method: "lp_rounding" as const, gap_bp: null, fallback_reason: "wall_clock_guard" as const } } : o,
    );
    const { container } = render(<ScorecardTable options={withFallback} />);
    const row = (id: string) => container.querySelector(`[data-option-id="${id}"]`) as HTMLElement;
    expect(within(row("opt_nearshore")).getByText("Whole units: CP-SAT, proven optimal")).toBeInTheDocument();
    expect(within(row("opt_hybrid")).getByText("Whole units: rounded from the LP (CP-SAT fallback: time guard)")).toBeInTheDocument();
    for (const id of ["opt_none", "opt_rebalance", "opt_sea_air"]) {
      expect(row(id).querySelector("[data-integer-method]")).toBeNull();
    }
    await expectNoA11yViolations(container);
  });

  it("the block renders the notes; the plain fixture has none", async () => {
    const onAction = vi.fn<(a: BlockAction) => Promise<void>>().mockResolvedValue(undefined);
    const r = render(<BlockRenderer envelope={fixture("scenario_compare.integer")} onAction={onAction} />);
    // scenario_compare is a lazily loaded canvas visual
    expect(await screen.findByText("Whole units: CP-SAT, best found (gap 0.4 %)", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(document.querySelectorAll("[data-integer-method]")).toHaveLength(2);
    await expectNoA11yViolations(r.container);
    r.unmount();
    render(<BlockRenderer envelope={fixture("scenario_compare")} onAction={onAction} />);
    expect(await screen.findByText("Scorecard by option", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(document.querySelector("[data-integer-method]")).toBeNull();
  });
});
