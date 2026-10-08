import type { BlockEnvelope } from "@keelson/contracts";
import { mergeBlockData } from "@keelson/viz";
import { fixtureData, getFixture } from "@keelson/viz/fixtures";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { fakeApi, jsonResponse, makeMe, withSession } from "../../../test/session";
import { CaseBlock } from "./CaseBlock";

const planDiff: BlockEnvelope = {
  id: "blk_plan",
  kind: "plan_diff",
  v: 1,
  title: "Changes in Merxa",
  placement: "canvas",
  provenance: { kind: "simulated" },
  data_ref: "blk_plan",
  props: { plan_version: 1, totals: { lines: 1, units: 300, value_cents: 270000 } },
};

const rows = [
  {
    seq: 1,
    op: "transfer_order.create",
    target_label: "Transfer DE1 → NL1, ships 2026-10-12",
    before_text: "No transfer order",
    after_text: "PUF-W-BLK-M × 120",
    qty: 120,
    system: "Merxa",
  },
];

const fixture = (name: string) => structuredClone(getFixture(name)!.envelope);

describe("CaseBlock data_ref", () => {
  it("merges the out-of-band payload over props (M1 kinds)", () => {
    expect(mergeBlockData(planDiff, { rows }).props).toEqual({ ...(planDiff.props as object), rows });
    expect(mergeBlockData({ ...planDiff, props: null }, [1, 2]).props).toEqual({ data: [1, 2] });
  });

  it("fetches GET /api/blocks/{id}/data before rendering the block", async () => {
    const { api, calls } = fakeApi([(url) => (url === "/api/blocks/blk_plan/data" ? jsonResponse(200, { rows }) : undefined)]);
    render(<CaseBlock envelope={planDiff} revision={1} onAction={() => {}} busy={false} density="canvas" />, {
      wrapper: withSession(makeMe(), api),
    });
    expect(screen.getByRole("status", { name: "Loading Changes in Merxa" })).toBeInTheDocument();
    expect(await screen.findByText("Transfer DE1 → NL1, ships 2026-10-12")).toBeInTheDocument();
    expect(calls.map((c) => c.url)).toEqual(["/api/blocks/blk_plan/data"]);
  });

  it("falls back to the raw-data card when the payload cannot be loaded", async () => {
    const { api } = fakeApi([]);
    render(<CaseBlock envelope={planDiff} revision={1} onAction={() => {}} busy={false} density="canvas" />, {
      wrapper: withSession(makeMe(), api),
    });
    expect(await screen.findByText(/Changes in Merxa/)).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Loading Changes in Merxa" })).not.toBeInTheDocument();
  });
});

describe("CaseBlock M3 canvas visuals (regression: data_ref payload is not merged into strict props)", { timeout: 30_000 }, () => {
  it("loads the network map payload from the block's own endpoint and renders the block", async () => {
    const envelope = fixture("network_map");
    const { api, calls } = fakeApi([
      (url) => (url === "/api/blocks/blk_network_map/data" ? jsonResponse(200, fixtureData("blk_network_map")) : undefined),
    ]);
    const { container } = render(<CaseBlock envelope={envelope} revision={1} onAction={() => {}} busy={false} density="canvas" />, {
      wrapper: withSession(makeMe(), api),
    });
    await waitFor(() => expect(container.querySelector('[data-block-kind="network_map"]')).not.toBeNull(), { timeout: 10_000 });
    expect(container.querySelector("[data-block-fallback]")).toBeNull();
    expect(screen.getByRole("table", { name: /^Sites/ })).toBeInTheDocument();
    expect(calls.map((c) => c.url)).toEqual(["/api/blocks/blk_network_map/data"]);
  });

  it("never fetches a foreign data_ref: always /api/blocks/{id}/data", async () => {
    const envelope = { ...fixture("impact_graph"), data_ref: "https://evil.example/steal" };
    const { api, calls } = fakeApi([
      (url) => (url === "/api/blocks/blk_impact_graph/data" ? jsonResponse(200, fixtureData("blk_impact_graph")) : undefined),
    ]);
    const { container } = render(<CaseBlock envelope={envelope} revision={1} onAction={() => {}} busy={false} density="canvas" />, {
      wrapper: withSession(makeMe(), api),
    });
    await waitFor(() => expect(container.querySelector('[data-block-kind="impact_graph"]')).not.toBeNull(), { timeout: 10_000 });
    expect(calls.map((c) => c.url)).toEqual(["/api/blocks/blk_impact_graph/data"]);
  });

  it("an invalid payload shows the raw-data card (invalid_data), a failed one offers Try again", async () => {
    const envelope = fixture("stockout_timeline");
    const { api } = fakeApi([(url) => (url.endsWith("/data") ? jsonResponse(200, { rows: "nope" }) : undefined)]);
    const { container, unmount } = render(
      <CaseBlock envelope={envelope} revision={1} onAction={() => {}} busy={false} density="canvas" />,
      { wrapper: withSession(makeMe(), api) },
    );
    await waitFor(() => expect(container.querySelector('[data-block-fallback="invalid_data"]')).not.toBeNull());
    unmount();
    const failing = fakeApi([(url) => (url.endsWith("/data") ? jsonResponse(500, { code: "boom", message: "boom" }) : undefined)]);
    render(<CaseBlock envelope={envelope} revision={1} onAction={() => {}} busy={false} density="canvas" />, {
      wrapper: withSession(makeMe(), failing.api),
    });
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("a re-emitted block (new revision) fetches its payload again", async () => {
    const envelope = fixture("stockout_timeline");
    const { api, calls } = fakeApi([(url) => (url.endsWith("/data") ? jsonResponse(200, fixtureData("blk_stockout_timeline")) : undefined)]);
    const wrapper = withSession(makeMe(), api);
    const { rerender, container } = render(
      <CaseBlock envelope={envelope} revision={1} onAction={() => {}} busy={false} density="canvas" />,
      { wrapper },
    );
    await waitFor(() => expect(container.querySelector('[data-block-kind="stockout_timeline"]')).not.toBeNull(), { timeout: 10_000 });
    rerender(<CaseBlock envelope={envelope} revision={2} onAction={() => {}} busy={false} density="canvas" />);
    await waitFor(() => expect(calls).toHaveLength(2), { timeout: 10_000 });
  });
});
