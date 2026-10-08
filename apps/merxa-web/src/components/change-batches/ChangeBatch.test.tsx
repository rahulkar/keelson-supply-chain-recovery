import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ChangeSetItem } from "@/lib/types";
import { StatusBadge } from "../ui/StatusBadge";
import { AttributionBanner } from "./AttributionBanner";
import { ItemDiffTable, targetHref } from "./ItemDiffTable";

const applied = {
  status: "applied" as const,
  approved_by: "user:marco",
  via: "Keelson",
  initiated_by: "Priya Shah",
  applied_by_display: "Marco Rossi",
  applied_by_username: "marco.rossi",
  approval_ref: "APR-0001",
  attribution_text: "Changed by Marco Rossi via Keelson (APR-0001)",
  applied_via_client: "keelson-connect",
};

describe("StatusBadge", () => {
  it("renders the humanized status with its tone", () => {
    render(<StatusBadge status="rolled_back" />);
    const badge = screen.getByText("Rolled back");
    expect(badge).toHaveClass("mx-badge", "mx-badge--neutral");
    expect(badge).toHaveAttribute("data-status", "rolled_back");
  });
});

describe("AttributionBanner", () => {
  it("shows the delegated attribution line in amber for external changes", () => {
    render(<AttributionBanner cs={applied} />);
    const banner = screen.getByTestId("attribution-banner");
    expect(banner).toHaveClass("mx-banner--external");
    expect(within(banner).getByText("Changed by Marco Rossi via Keelson (APR-0001)")).toBeInTheDocument();
    expect(within(banner).getByText("marco")).toBeInTheDocument();
    expect(within(banner).getByText("Initiated by Priya Shah")).toBeInTheDocument();
    expect(within(banner).getByText("keelson-connect")).toBeInTheDocument();
  });

  it("shows policy attribution", () => {
    render(
      <AttributionBanner
        cs={{
          ...applied,
          approved_by: "policy:P-12@3",
          attribution_text: "Applied via Keelson auto-policy P-12@3, initiated by Priya Shah",
          applied_via_client: "keelson-autocommit",
        }}
      />,
    );
    expect(screen.getByText("Applied via Keelson auto-policy P-12@3, initiated by Priya Shah")).toBeInTheDocument();
    expect(screen.getByText("P-12")).toBeInTheDocument();
  });

  it("explains batches that were not applied", () => {
    render(<AttributionBanner cs={{ ...applied, status: "validated", attribution_text: null }} />);
    expect(screen.getByText("Not applied")).toBeInTheDocument();
    expect(screen.queryByTestId("attribution-banner")).not.toBeInTheDocument();
  });
});

describe("ItemDiffTable", () => {
  const items: ChangeSetItem[] = [
    {
      seq: 1,
      op: "transfer_order.create",
      target_ref: null,
      expected_version: null,
      payload: { from_dc: "DE1", to_dc: "NL1", ship_date: "2026-10-12", lines: [{ sku: "PUF-W-BLK-M", qty: 300 }] },
      before: { transfer_order: null, from_dc: "DE1", to_dc: "NL1", lines: [{ sku: "PUF-W-BLK-M", from_atp: 900, to_in_transit: 0 }] },
      after: {
        transfer_order: { from_dc: "DE1", to_dc: "NL1", ship_date: "2026-10-12", eta_date: "2026-10-14", status: "planned" },
        from_dc: "DE1",
        to_dc: "NL1",
        lines: [{ sku: "PUF-W-BLK-M", qty: 300, from_atp: 600, to_in_transit: 300 }],
      },
      status: "applied",
      error: null,
    },
    {
      seq: 2,
      op: "po_line.reschedule",
      target_ref: "PO-BD-0001#2",
      expected_version: 1,
      payload: { eta_dc: "2026-11-05" },
      before: null,
      after: null,
      status: "error",
      error: "stale",
    },
  ];

  it("renders before/after per item and highlights changed fields", () => {
    render(<ItemDiffTable items={items} conflicts={[{ seq: 2, target_ref: "PO-BD-0001#2", expected_version: 1, actual_version: 2, reason: "version_mismatch" }]} />);
    const first = screen.getByRole("region", { name: "Item 1: Create transfer order" });
    const atpRow = within(first).getByText("PUF-W-BLK-M · from-DC ATP").closest("tr")!;
    expect(atpRow).toHaveClass("is-changed");
    expect(within(atpRow).getByText("900")).toBeInTheDocument();
    expect(within(atpRow).getByText("600")).toBeInTheDocument();
    expect(within(atpRow).getByText("-300")).toBeInTheDocument();
    const fromRow = within(first).getByText("from DC").closest("tr")!;
    expect(fromRow).not.toHaveClass("is-changed");

    const second = screen.getByRole("region", { name: "Item 2: Reschedule PO line" });
    expect(within(second).getByRole("alert")).toHaveTextContent("Conflict: version_mismatch (expected v1, found v2)");
    expect(within(second).getByRole("link", { name: "PO-BD-0001#2" })).toHaveAttribute("href", "/purchase-orders/PO-BD-0001");
    expect(within(second).getByText(/No before\/after values were recorded/)).toBeInTheDocument();
  });

  it("links PO-line targets only", () => {
    expect(targetHref({ op: "po_line.change_qty", target_ref: "PO-KH-0001#4" })).toBe("/purchase-orders/PO-KH-0001");
    expect(targetHref({ op: "allocation.set_qty", target_ref: "AL-000123" })).toBeNull();
    expect(targetHref({ op: "transfer_order.create", target_ref: null })).toBeNull();
  });

  it("shows an empty state", () => {
    render(<ItemDiffTable items={[]} />);
    expect(screen.getByText("This batch has no items.")).toBeInTheDocument();
  });
});
