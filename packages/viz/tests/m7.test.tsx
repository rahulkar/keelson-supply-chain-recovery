import { blockEnvelopeSchema, type BlockEnvelope } from "@keelson/contracts";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ADVISORY_FALLBACK_TEXT,
  AdvisoryNote,
  BlockRenderer,
  WORKFLOW_FIXED_NODES,
  advisoryRangeText,
  buildWorkflowModel,
  specialistNodeId,
  workflowNodeAriaLabel,
  type BlockAction,
} from "../src";
import { blockFixtures, getFixture } from "../src/fixtures";
import { expectNoA11yViolations } from "./a11y";

const fixture = (name: string): BlockEnvelope => {
  const f = getFixture(name);
  if (!f) throw new Error(`missing fixture ${name}`);
  return structuredClone(f.envelope);
};

const renderBlock = (envelope: BlockEnvelope) => {
  const onAction = vi.fn<(a: BlockAction) => Promise<void>>().mockResolvedValue(undefined);
  return render(<BlockRenderer envelope={envelope} onAction={onAction} />);
};

describe("M7 advisory label (plans on crawled data)", () => {
  it("fixtures are appended after the M6 ones and validate against the envelope contract", () => {
    const names = blockFixtures.map((f) => f.name);
    const m6 = names.indexOf("alert_origin.feed");
    expect(names.slice(m6 + 1, m6 + 4)).toEqual(["recommended_option.advisory", "narrative.advisory", "scenario_compare.advisory"]);
    for (const n of ["recommended_option.advisory", "narrative.advisory", "scenario_compare.advisory"]) {
      expect(blockEnvelopeSchema.safeParse(fixture(n)).success, n).toBe(true);
    }
  });

  it("recommended option: Advisory badge in the header and the sentence with the as-of range", async () => {
    const { container } = renderBlock(fixture("recommended_option.advisory"));
    const card = screen.getByRole("region", { name: /Recommended|Rebalance|Move/i });
    expect(within(card).getByText("Advisory")).toBeInTheDocument();
    const note = within(card).getByRole("note");
    expect(note).toHaveTextContent("Advisory: Data gathered by paging, not a consistent snapshot.");
    expect(note).toHaveTextContent("Data read between 5 Oct 2026, 09:12 UTC and 5 Oct 2026, 09:19 UTC.");
    await expectNoA11yViolations(container);
  });

  it("narrative and scenario compare carry the note; plans without the label show none", async () => {
    const { unmount } = renderBlock(fixture("narrative.advisory"));
    expect(document.querySelector("[data-advisory]")).toHaveTextContent("not a consistent snapshot");
    unmount();
    // scenario_compare is a lazily loaded canvas visual.
    const r = renderBlock(fixture("scenario_compare.advisory"));
    expect(await screen.findByText("Advisory", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(document.querySelector("[data-advisory]")).not.toBeNull();
    r.unmount();
    renderBlock(fixture("recommended_option"));
    expect(document.querySelector("[data-advisory]")).toBeNull();
    expect(screen.queryByText("Advisory")).toBeNull();
  });

  it("an empty text falls back to the contract sentence; equal range ends read as one time", () => {
    render(<AdvisoryNote advisory={{ reason: "crawled_snapshot", text: " ", as_of_range: { from: "2026-10-05T09:12:00Z", to: "2026-10-05T09:12:00Z" } }} />);
    expect(screen.getByRole("note")).toHaveTextContent(`${ADVISORY_FALLBACK_TEXT}. Data read at 5 Oct 2026, 09:12 UTC.`);
    expect(advisoryRangeText({ from: "bad", to: "bad" })).toBe("Data read at bad");
  });
});

describe("M7 workflow graph model", () => {
  it("fans specialists out between investigate and verify_numbers; disabled ones are dashed", () => {
    const model = buildWorkflowModel({
      specialists: [
        { slug: "inventory_demand", label: "Inventory & demand analyst", custom: false, enabled: true, connectors: ["merxa"], tools: 7 },
        { slug: "supplier_risk", label: "Supplier risk analyst", custom: true, enabled: false, connectors: ["merxa-lite", "signal-hub"], tools: 3, knowledgeBases: 1 },
        { slug: "supplier_risk", label: "duplicate", custom: true, enabled: true, connectors: [], tools: 0 },
      ],
    });
    const ids = model.nodes.map((n) => n.id);
    expect(ids).toEqual([...WORKFLOW_FIXED_NODES.map((n) => n.id), specialistNodeId("inventory_demand"), specialistNodeId("supplier_risk")]);
    const custom = model.nodes.find((n) => n.id === "spec:supplier_risk")!;
    expect(custom).toMatchObject({ kind: "specialist", enabled: false, custom: true, stage: "checking_systems" });
    expect(custom.detail).toBe("Custom · reads merxa-lite, signal-hub (3 tools) · 1 knowledge base");
    expect(workflowNodeAriaLabel(custom)).toBe("Supplier risk analyst, Specialist, custom, disabled, Custom · reads merxa-lite, signal-hub (3 tools) · 1 knowledge base");
    expect(model.edges).toContainEqual({ source: "investigate", target: "spec:supplier_risk", dashed: true });
    expect(model.edges).toContainEqual({ source: "spec:inventory_demand", target: "verify_numbers", dashed: false });
    expect(model.edges).not.toContainEqual({ source: "investigate", target: "verify_numbers" });
    expect(model.edges).toContainEqual({ source: "compose", target: "compile_changeset", label: "Apply changes", dashed: true });
  });

  it("stage toggles switch fixed nodes off; no specialists → investigate goes straight on", () => {
    const model = buildWorkflowModel({ specialists: [], stages: { critic: false, unknown: false } });
    expect(model.nodes.find((n) => n.id === "critic")?.enabled).toBe(false);
    expect(model.nodes.filter((n) => !n.enabled).map((n) => n.id)).toEqual(["critic"]);
    expect(workflowNodeAriaLabel(model.nodes.find((n) => n.id === "critic")!)).toContain("switched off");
    expect(model.edges).toContainEqual({ source: "investigate", target: "verify_numbers" });
    // Every edge joins existing nodes.
    const ids = new Set(model.nodes.map((n) => n.id));
    for (const e of model.edges) expect(ids.has(e.source) && ids.has(e.target), `${e.source}>${e.target}`).toBe(true);
  });
});
