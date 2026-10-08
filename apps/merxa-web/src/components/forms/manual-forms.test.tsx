import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Allocation, CapacityBookingDetail, PoLine } from "@/lib/types";
import { DemoDirector, percentToBp } from "../demo/DemoDirector";
import {
  AllocationQtyForm,
  CancelBookingForm,
  CapacityBookingForm,
  CreateTransferOrder,
  PoLineEditForm,
} from "./ManualForms";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/transfer-orders/new",
}));

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

type FetchCall = [string, RequestInit];
const calls = (m: ReturnType<typeof vi.fn>) => m.mock.calls as unknown as FetchCall[];
const header = (init: RequestInit, name: string) => (init.headers as Record<string, string>)[name];

beforeEach(() => refresh.mockClear());
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const MANUAL = { source: "manual", via: "Merxa UI", changed_by: { username: "priya.shah", display: "Priya Shah" } };

describe("CreateTransferOrder", () => {
  const lines = [
    { sku: "PUF-W-BLK-S", qty: 7 },
    { sku: "PUF-W-BLK-M", qty: 5 },
  ];

  it("creates the transfer order with an Idempotency-Key and links to it", async () => {
    const fetchMock = vi.fn(async () =>
      json(
        {
          ...MANUAL,
          event: "transfer_order.created",
          entity_type: "transfer_order",
          entity_ref: "TO-000123",
          code: "TO-000123",
          from_dc: "DE1",
          to_dc: "NL1",
          ship_date: "2026-10-14",
          eta_date: "2026-10-16",
          status: "planned",
          lines: [],
          units: 12,
          version: 1,
        },
        201,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<CreateTransferOrder from="DE1" to="NL1" shipDate="2026-10-14" lines={lines} problem={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Create transfer order" }));
    const link = await screen.findByRole("link", { name: "TO-000123" });
    expect(link).toHaveAttribute("href", "/transfer-orders?code=TO-000123");
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/transfer-orders");
    expect(init.method).toBe("POST");
    expect(header(init, "idempotency-key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(JSON.parse(String(init.body))).toEqual({ from_dc: "DE1", to_dc: "NL1", ship_date: "2026-10-14", lines });
    expect(refresh).toHaveBeenCalled();
    // created once: the button cannot submit a second transfer order
    expect(screen.getByRole("button", { name: "Created TO-000123" })).toBeDisabled();
  });

  it("retries an ambiguous failure with the same key, a refused one with a new key", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(json({ status: 422, code: "validation_failed", detail: "insufficient ATP for PUF-W-BLK-S" }, 422))
      .mockResolvedValueOnce(json({ status: 503, code: "unavailable", detail: "down" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    render(<CreateTransferOrder from="DE1" to="NL1" shipDate="2026-10-14" lines={lines} problem={null} />);
    const button = screen.getByRole("button", { name: "Create transfer order" });
    await userEvent.click(button);
    expect(await screen.findByText(/Saving again is safe/)).toBeInTheDocument();
    await userEvent.click(button);
    expect(await screen.findByText("insufficient ATP for PUF-W-BLK-S")).toBeInTheDocument();
    await userEvent.click(button);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const keys = calls(fetchMock).map(([, init]) => header(init, "idempotency-key"));
    expect(keys[1]).toBe(keys[0]); // network error: outcome unknown → same key (merxa-api replays)
    expect(keys[2]).not.toBe(keys[1]); // 422: nothing was applied → fresh key
  });

  it("does not submit while the planner reports a problem", () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<CreateTransferOrder from="DE1" to="DE1" shipDate="2026-10-14" lines={lines} problem="From and To DC must differ." />);
    expect(screen.getByRole("button", { name: "Create transfer order" })).toBeDisabled();
    expect(screen.getByText("From and To DC must differ.")).toBeInTheDocument();
  });
});

const ALLOC: Allocation = {
  code: "AL-000042",
  sku: "PUF-W-BLK-M",
  dc_code: "DE1",
  store_code: "DE-BER-001",
  ship_week: "2026-W42",
  qty: 10,
  status: "planned",
  version: 4,
};

describe("AllocationQtyForm", () => {
  it("is pre-filled from the guide and saves with If-Match on the current version", async () => {
    const fetchMock = vi.fn(async () =>
      json({ ...MANUAL, event: "allocation.updated", entity_type: "allocation", entity_ref: ALLOC.code, code: ALLOC.code, qty: 14, qty_before: 10, version: 5 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<AllocationQtyForm allocation={ALLOC} initialQty={14} planned={4} />);
    expect(screen.getByRole("textbox")).toHaveValue("14");
    expect(screen.queryByText(/planned against version/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save quantity" }));
    expect(await screen.findByText(/quantity 10 → 14 \(version 5\)/)).toBeInTheDocument();
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/allocations/AL-000042");
    expect(init.method).toBe("PATCH");
    expect(header(init, "if-match")).toBe('"4"');
    expect(header(init, "idempotency-key")).toBeTruthy();
    expect(JSON.parse(String(init.body))).toEqual({ qty: 14 });
  });

  it("warns when the record changed since the step was planned and reloads after a 412", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ status: 412, code: "precondition_failed", detail: "changed" }, 412)));
    render(<AllocationQtyForm allocation={ALLOC} initialQty={14} planned={2} />);
    expect(screen.getByText(/planned against version 2; the record is now at version 4/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save quantity" }));
    expect(await screen.findByText(/changed by someone else/)).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("rejects unreadable quantities and locked allocations locally", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { unmount } = render(<AllocationQtyForm allocation={ALLOC} initialQty={null} planned={null} />);
    const input = screen.getByRole("textbox");
    expect(input).toHaveValue("10");
    await userEvent.clear(input);
    await userEvent.type(input, "1.5");
    expect(screen.getByRole("button", { name: "Save quantity" })).toBeDisabled();
    unmount();
    render(<AllocationQtyForm allocation={{ ...ALLOC, status: "shipped" }} initialQty={3} planned={null} />);
    expect(screen.getByRole("button", { name: "Save quantity" })).toBeDisabled();
    expect(screen.getByText(/status shipped cannot be changed/)).toBeInTheDocument();
  });
});

const LINE: PoLine = {
  line_no: 2,
  sku: "PUF-W-BLK-L",
  qty_ordered: 400,
  qty_received: 50,
  eta_dc: "2026-11-02",
  status: "in_production",
  version: 3,
};

describe("PoLineEditForm", () => {
  it("reschedules a line with the guide's ETA", async () => {
    const fetchMock = vi.fn(async () =>
      json({ ...MANUAL, event: "po_line.updated", entity_type: "po_line", entity_ref: "PO-BD-0002#2", ref: "PO-BD-0002#2", action: "reschedule", eta_dc: "2026-11-20", qty_ordered: 400, version: 4 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<PoLineEditForm poCode="PO-BD-0002" line={LINE} action="reschedule" initialEta="2026-11-20" initialQty={null} planned={3} />);
    await userEvent.click(screen.getByRole("button", { name: "Save line 2" }));
    expect(await screen.findByText(/now arrives 2026-11-20/)).toBeInTheDocument();
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/purchase-orders/PO-BD-0002/lines/2");
    expect(init.method).toBe("PATCH");
    expect(header(init, "if-match")).toBe('"3"');
    expect(JSON.parse(String(init.body))).toEqual({ eta_dc: "2026-11-20" });
  });

  it("changes the ordered quantity, never below what was received", async () => {
    const fetchMock = vi.fn(async () =>
      json({ ...MANUAL, event: "po_line.updated", entity_type: "po_line", entity_ref: "PO-BD-0002#2", ref: "PO-BD-0002#2", action: "change_qty", eta_dc: "2026-11-02", qty_ordered: 300, version: 4 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<PoLineEditForm poCode="PO-BD-0002" line={LINE} action="change-qty" initialEta={null} initialQty={30} planned={null} />);
    const save = screen.getByRole("button", { name: "Save line 2" });
    expect(save).toBeDisabled(); // 30 < 50 received
    const input = screen.getByRole("textbox");
    await userEvent.clear(input);
    await userEvent.type(input, "300");
    await userEvent.click(save);
    expect(await screen.findByText(/ordered quantity is 300/)).toBeInTheDocument();
    expect(JSON.parse(String((calls(fetchMock)[0] as FetchCall)[1].body))).toEqual({ qty_ordered: 300 });
  });
});

describe("Capacity bookings", () => {
  it("books the guide's lane, week and units and links to the booking", async () => {
    const fetchMock = vi.fn(async () =>
      json(
        { ...MANUAL, event: "capacity_booking.created", entity_type: "capacity_booking", entity_ref: "CB-0107", code: "CB-0107", lane_code: "CGP-UK1-SA", iso_week: "2026-W44", qty_units: 120, status: "confirmed", version: 1 },
        201,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CapacityBookingForm
        lanes={[{ code: "CGP-UK1-SA", label: "CGP-UK1-SA · BDCGP → UK1 (sea-air)" }]}
        initialLane="CGP-UK1-SA"
        initialWeek="2026-W44"
        initialQty={120}
        remainingByLaneWeek={{ "CGP-UK1-SA|2026-W44": 900 }}
      />,
    );
    expect(screen.getByText("Remaining capacity in 2026-W44: 900 units.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Book capacity" }));
    expect(await screen.findByRole("link", { name: "CB-0107" })).toHaveAttribute("href", "/capacity-bookings/CB-0107");
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/capacity-bookings");
    expect(JSON.parse(String(init.body))).toEqual({ lane_code: "CGP-UK1-SA", iso_week: "2026-W44", qty_units: 120 });
    expect(header(init, "idempotency-key")).toBeTruthy();
  });

  it("does not offer remaining capacity it could not compute", () => {
    render(
      <CapacityBookingForm lanes={[{ code: "X", label: "X" }]} initialLane="X" initialWeek="2026-W44" initialQty={null} remainingByLaneWeek={{ "X|2026-W44": null }} />,
    );
    expect(screen.getByText(/No rate card offers capacity/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Book capacity" })).toBeDisabled(); // no units yet
  });

  const BOOKING: CapacityBookingDetail = {
    code: "CB-0003",
    lane_code: "CGP-UK1-SA",
    iso_week: "2026-W43",
    qty_units: 800,
    basis: "per_unit",
    status: "confirmed",
    expires_at: null,
    created_via: "ui",
    version: 2,
    shipments: [{ code: "SHP-000004", po_code: "PO-BD-0004", mode: "sea_air", status: "booked", etd: "2026-10-20", eta: "2026-10-27" }],
    lane_week: { remaining_capacity: 0, weekly_capacity: 800, basis: "per_unit" },
    today: "2026-10-05",
  };

  it("cancels a booking with If-Match after confirming (linked shipments named)", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = vi.fn(async () =>
      json({ ...MANUAL, event: "capacity_booking.cancelled", entity_type: "capacity_booking", entity_ref: "CB-0003", code: "CB-0003", lane_code: "CGP-UK1-SA", iso_week: "2026-W43", qty_units: 800, status: "released", version: 3 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<CancelBookingForm booking={BOOKING} planned={2} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel booking" }));
    expect(await screen.findByText(/Booking CB-0003 cancelled/)).toBeInTheDocument();
    expect(confirm.mock.calls[0]?.[0]).toMatch(/SHP-000004/);
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/capacity-bookings/CB-0003:cancel");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    expect(header(init, "if-match")).toBe('"2"');
  });

  it("does nothing for a released booking or when the confirmation is declined", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { unmount } = render(<CancelBookingForm booking={BOOKING} planned={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel booking" }));
    expect(fetchMock).not.toHaveBeenCalled();
    unmount();
    render(<CancelBookingForm booking={{ ...BOOKING, status: "released" }} planned={null} />);
    expect(screen.getByRole("button", { name: "Cancel booking" })).toBeDisabled();
  });

  it("exposes the button-only forms as labelled groups (aria-label needs a role on a div)", () => {
    render(<CancelBookingForm booking={BOOKING} planned={null} />);
    expect(screen.getByRole("group", { name: "Cancel booking" })).toBeInTheDocument();
    render(<CreateTransferOrder from="DE1" to="NL1" shipDate="2026-10-14" lines={[{ sku: "PUF-W-BLK-S", qty: 1 }]} problem={null} />);
    expect(screen.getByRole("group", { name: "Create transfer order" })).toBeInTheDocument();
  });
});

describe("Demo Director: lane capacity", () => {
  it("converts the capacity percentage to basis points", () => {
    expect(percentToBp("30")).toBe(3000);
    expect(percentToBp("0")).toBe(0);
    expect(percentToBp("150")).toBe(15000);
    expect(percentToBp("30.5")).toBeNull();
    expect(percentToBp("1001")).toBeNull();
  });

  it("injects the lane cut with an Idempotency-Key", async () => {
    const fetchMock = vi.fn(async () =>
      json({ kind: "lane_capacity", lane_code: "CGP-UK1-SA", factor_bp: 3000, from_week: "2026-W41", to_week: "2026-W44", effective_capacity_units: 240 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<DemoDirector openPoCodes={["PO-BD-0001"]} laneCodes={["CGP-UK1-SA", "CGP-NL1-SA"]} />);
    await userEvent.type(screen.getByLabelText("Carrier note (optional)"), "Space cut");
    await userEvent.click(screen.getByRole("button", { name: "Apply cut" }));
    expect(await screen.findByText(/Capacity on CGP-UK1-SA set to 30% for 2026-W41–2026-W44 \(now 240 units\/week\)/)).toBeInTheDocument();
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/admin/scenario:inject");
    expect(header(init, "idempotency-key")).toBeTruthy();
    expect(JSON.parse(String(init.body))).toEqual({
      kind: "lane_capacity",
      payload: { lane_code: "CGP-UK1-SA", factor_bp: 3000, weeks: 4, carrier_note: "Space cut" },
    });
  });
});
