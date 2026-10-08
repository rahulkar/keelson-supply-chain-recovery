import { alertOriginPropsSchema, type BlockEnvelope } from "@keelson/contracts";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  BlockRenderer,
  DemoBadge,
  KindIcon,
  SeverityBadge,
  alertHref,
  blockRegistry,
  humanize,
  kindLabel,
  severityRank,
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

describe("M6 fixtures", () => {
  it("are appended after the M5 ones and validate against the contract", () => {
    const names = blockFixtures.map((f) => f.name);
    // M7 appends its advisory fixtures after these (see m7.test.tsx).
    const at = names.indexOf("alert_origin");
    expect(names.slice(at, at + 2)).toEqual(["alert_origin", "alert_origin.feed"]);
    expect(names.indexOf("alert_origin")).toBeGreaterThan(names.indexOf("narrative.replan"));
    for (const name of ["alert_origin", "alert_origin.feed"]) {
      expect(alertOriginPropsSchema.safeParse(fixture(name).props).success).toBe(true);
    }
    expect(blockRegistry["alert_origin@1"]).toBeDefined();
  });
});

describe("alert_origin@1", () => {
  it("shows the alert title, severity, kind, sources, time, DEMO badge and a link to the alert", async () => {
    const { container } = renderBlock(fixture("alert_origin"));
    const card = screen.getByRole("region", { name: "Opened from alert" });
    expect(within(card).getByText("Tropical cyclone approaching Ho Chi Minh City: Cat Lai terminal at risk")).toBeInTheDocument();
    expect(within(card).getByText("Critical")).toBeInTheDocument();
    expect(within(card).getByText("Natural hazard")).toBeInTheDocument();
    expect(within(card).getByText("DEMO")).toBeInTheDocument();
    expect(within(card).getByText(/GDACS · Demo Freight Daily/)).toBeInTheDocument();
    expect(within(card).getByText(/Case opened 8 Oct 2026, 06:00 UTC/)).toBeInTheDocument();
    const link = within(card).getByRole("link", { name: "View the alert" });
    expect(link).toHaveAttribute("href", "/alerts?alert=3f8e2a64-5b1c-5d7e-9a0b-1c2d3e4f5a6b");
    expect(container.querySelector("[data-block-kind='alert_origin']")).not.toBeNull();
    await expectNoA11yViolations(container);
  });

  it("a live feed alert has no DEMO badge and a single source", async () => {
    const { container } = renderBlock(fixture("alert_origin.feed"));
    const card = screen.getByRole("region", { name: "Opened from alert" });
    expect(within(card).queryByText("DEMO")).toBeNull();
    expect(within(card).getByText("High")).toBeInTheDocument();
    expect(within(card).getByText("Source:")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("renders a headline with markup as plain text (untrusted news text)", () => {
    const env = fixture("alert_origin");
    (env.props as { title: string }).title = "<img src=x onerror=alert(1)> **bold** [link](http://evil.test)";
    const { container } = renderBlock(env);
    expect(screen.getByText("<img src=x onerror=alert(1)> **bold** [link](http://evil.test)")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  it("falls back to the raw card for an invalid alert id (never builds a link from it)", () => {
    const env = fixture("alert_origin");
    (env.props as { alert_id: string }).alert_id = "../../api/demo/reset";
    renderBlock(env);
    expect(screen.queryByRole("link", { name: "View the alert" })).toBeNull();
  });
});

describe("alert vocabulary", () => {
  it("labels kinds and severities in words", async () => {
    expect(kindLabel("labor")).toBe("Labour action");
    expect(kindLabel("port_issue")).toBe("Port issue");
    expect(kindLabel("constructor")).toBe("Constructor");
    expect(humanize("tropical_cyclone")).toBe("Tropical cyclone");
    expect(severityRank("critical")).toBeGreaterThan(severityRank("high"));
    expect(severityRank("high")).toBeGreaterThan(severityRank("medium"));
    expect(severityRank("toString")).toBe(-1);
    expect(alertHref("a b")).toBe("/alerts?alert=a%20b");
    const { container } = render(
      <div>
        <SeverityBadge severity="medium" />
        <KindIcon kind="logistics" label="Logistics" />
        <KindIcon kind="unknown_kind" />
        <DemoBadge />
      </div>,
    );
    expect(screen.getByText("Medium")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Logistics" })).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });
});
