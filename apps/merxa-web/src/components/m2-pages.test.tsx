import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WEBHOOK_EVENT_TYPES, capacityMatrix } from "@/lib/m2";
import type {
  ApiKey,
  CapacityBooking,
  FactorySchedule,
  Lane,
  OAuthClientSeen,
  OnlineOrder,
  RateCard,
  SupplierMessage,
  WebhookSubscription,
} from "@/lib/types";
import { ApiClients } from "./admin/ApiClients";
import { unlessPinned } from "./grid/cells";
import { Webhooks } from "./admin/Webhooks";
import { CapacityTable } from "./logistics/CapacityTable";
import { OnlineOrderGrid } from "./demand/DemandGrids";
import { DataGrid } from "./grid/DataGrid";
import { MAX_TRACKS, ProductionSchedule, weekSegments } from "./production/ProductionSchedule";
import { Sidebar } from "./shell/Sidebar";
import { SupplierInbox } from "./supplier/SupplierInbox";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/sailings",
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

describe("Sidebar (M2 modules)", () => {
  it("lists the new logistics, production and demand modules for everyone", () => {
    render(<Sidebar showDemoDirector={false} />);
    const nav = screen.getByRole("navigation", { name: "Modules" });
    for (const name of ["Sailings & Lanes", "Shipments", "Production Schedule", "Online Orders", "Promotions", "Supplier Portal"]) {
      expect(within(nav).getByRole("link", { name })).toHaveAttribute("title", name);
    }
    expect(within(nav).getByRole("link", { name: "Sailings & Lanes" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).queryByRole("link", { name: "API & MCP Clients" })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Webhooks" })).not.toBeInTheDocument();
  });

  it("shows the administration modules to Merxa administrators only", () => {
    render(<Sidebar showDemoDirector showAdmin />);
    const nav = screen.getByRole("navigation", { name: "Modules" });
    expect(within(nav).getByRole("link", { name: "API & MCP Clients" })).toHaveAttribute("href", "/api-clients");
    expect(within(nav).getByRole("link", { name: "Webhooks" })).toHaveAttribute("href", "/webhooks");
    expect(within(nav).getByRole("link", { name: "Demo Director" })).toBeInTheDocument();
  });
});

// ───────────────────────── API & MCP Clients ─────────────────────────
const KEY: ApiKey = {
  id: "0199aaaa-0000-7000-8000-000000000001",
  name: "Finance BI",
  prefix: "ab12cd34",
  scopes: ["catalog:read", "po:read"],
  created_by: "anna.admin",
  created_at: "2026-10-01T09:00:00Z",
  last_used_at: null,
  revoked_at: null,
  version: 1,
  status: "active",
};
const READER: OAuthClientSeen = {
  client_id: "keelson-reader",
  last_seen_at: "2026-10-06T08:00:00Z",
  last_sub: "service-account-keelson-reader",
  last_username: "service-account-keelson-reader",
  surface: "mcp",
};
const SECRET_KEY = "mx_zz99yy88_AbCdEfGhIjKlMnOpQrStUvWxYz012345";

describe("ApiClients", () => {
  it("lists keys masked and the connected OAuth clients with last-seen", () => {
    render(<ApiClients initialKeys={[KEY]} clients={[READER]} clientsError={null} />);
    const keys = screen.getByRole("table", { name: "API keys" });
    expect(within(keys).getByText("mx_ab12cd34_••••••••")).toBeInTheDocument();
    expect(within(keys).getByText("never")).toBeInTheDocument();
    const clients = screen.getByRole("table", { name: "Connected OAuth clients" });
    expect(within(clients).getByText("keelson-reader")).toBeInTheDocument();
    expect(within(clients).getByText("Keelson read-only service principal")).toBeInTheDocument();
    expect(within(clients).getByText("MCP (/mcp)")).toBeInTheDocument();
  });

  it("mints a key with an Idempotency-Key, shows it once and lists it masked", async () => {
    const fetchMock = vi.fn(async () =>
      json(
        {
          id: "0199aaaa-0000-7000-8000-000000000002",
          name: "Ops dashboard",
          prefix: "zz99yy88",
          scopes: ["inventory:read", "po:read"],
          created_by: "anna.admin",
          created_at: "2026-10-06T10:00:00Z",
          key: SECRET_KEY,
          key_shown_once: true,
        },
        201,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ApiClients initialKeys={[KEY]} clients={[]} clientsError={null} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Ops dashboard");
    // Default grant: the four plain read scopes. Keep inventory + po only.
    await userEvent.click(screen.getByRole("checkbox", { name: /catalog:read/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /orders:read/ }));
    await userEvent.click(screen.getByRole("button", { name: /Mint key/ }));

    const shown = await screen.findByRole("textbox", { name: /API key “Ops dashboard”/ });
    expect(shown).toHaveValue(SECRET_KEY);
    expect(screen.getByText(/shown only once/)).toBeInTheDocument();
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/api-keys");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ name: "Ops dashboard", scopes: ["inventory:read", "po:read"] });
    expect(header(init, "idempotency-key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(within(screen.getByRole("table", { name: "API keys" })).getByText("mx_zz99yy88_••••••••")).toBeInTheDocument();

    // Dismissed: the secret is gone from the page for good.
    await userEvent.click(screen.getByRole("button", { name: "I have stored it" }));
    expect(screen.queryByDisplayValue(SECRET_KEY)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET_KEY);
  });

  it("reuses the Idempotency-Key when retrying after an ambiguous failure", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ title: "Bad gateway", status: 502 }, 502))
      .mockResolvedValueOnce(
        json({ id: "k3", name: "Retry", prefix: "aaaabbbb", scopes: ["po:read"], created_by: "a", created_at: "2026-10-06T10:00:00Z", key: null, key_shown_once: false }, 201),
      );
    vi.stubGlobal("fetch", fetchMock);
    render(<ApiClients initialKeys={[]} clients={[]} clientsError={null} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Retry");
    await userEvent.click(screen.getByRole("button", { name: /Mint key/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Bad gateway");
    await userEvent.click(screen.getByRole("button", { name: /Mint key/ }));
    // A replay has no key: the page says so instead of showing an empty secret.
    expect(await screen.findByText(/cannot be shown again/)).toBeInTheDocument();
    const [first, second] = calls(fetchMock);
    expect(header((first as FetchCall)[1], "idempotency-key")).toBe(header((second as FetchCall)[1], "idempotency-key"));
  });

  it("does not resolve client ids or surfaces through Object.prototype", () => {
    render(
      <ApiClients
        initialKeys={[]}
        clients={[{ ...READER, client_id: "constructor", surface: "toString" }]}
        clientsError={null}
      />,
    );
    const row = within(screen.getByRole("table", { name: "Connected OAuth clients" })).getByText("constructor").closest("tr") as HTMLElement;
    const cells = within(row).getAllByRole("cell");
    expect(cells[1]).toHaveTextContent("—");
    expect(cells[3]).toHaveTextContent("toString");
  });

  it("validates the form before calling the API", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<ApiClients initialKeys={[]} clients={[]} clientsError={null} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "No scopes");
    for (const s of ["catalog:read", "inventory:read", "po:read", "orders:read"]) {
      await userEvent.click(screen.getByRole("checkbox", { name: new RegExp(s) }));
    }
    await userEvent.click(screen.getByRole("button", { name: /Mint key/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("Select at least one scope.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("revokes a key after confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = vi.fn(async () => json({ ...KEY, revoked_at: "2026-10-06T11:00:00Z", status: "revoked" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ApiClients initialKeys={[KEY]} clients={[]} clientsError={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Revoke Finance BI" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Revoke Finance BI" })).not.toBeInTheDocument());
    expect(screen.getByText("Revoked")).toBeInTheDocument();
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe(`/v1/api-keys/${KEY.id}:revoke`);
    expect(init.method).toBe("POST");
  });

  it("does nothing when the revoke is not confirmed", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<ApiClients initialKeys={[KEY]} clients={[]} clientsError={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Revoke Finance BI" }));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ───────────────────────── Webhooks ─────────────────────────
const SUB: WebhookSubscription = {
  id: "0199bbbb-0000-7000-8000-000000000001",
  name: "Keelson (demo-retail)",
  url: "http://keelson.localhost/api/webhooks/demo-retail/merxa",
  events: ["change_set.applied", "po_line.eta_changed"],
  active: true,
  created_by: "system",
  created_at: "2026-10-01T09:00:00Z",
  version: 3,
  delivered: 12,
  pending: 1,
  failed: 2,
  last_delivered_at: "2026-10-06T09:00:00Z",
};

describe("Webhooks", () => {
  it("shows subscriptions with delivery counts and the endpoint as plain text", () => {
    render(<Webhooks subscriptions={[{ ...SUB, url: "javascript:alert(1)" }]} deliveries={[]} deliveriesError={null} />);
    const table = screen.getByRole("table", { name: "Webhook subscriptions" });
    expect(within(table).getByText("javascript:alert(1)").tagName).toBe("TD");
    expect(within(table).queryByRole("link")).not.toBeInTheDocument();
    expect(within(table).getByText("Active")).toBeInTheDocument();
    expect(within(table).getByText("12")).toBeInTheDocument();
  });

  it("pauses a subscription with If-Match on its version", async () => {
    const fetchMock = vi.fn(async () => json({ ...SUB, active: false, version: 4 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: `Pause ${SUB.name}` }));
    await waitFor(() => expect(refresh).toHaveBeenCalled());
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe(`/v1/webhook-subscriptions/${SUB.id}`);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ active: false });
    expect(header(init, "if-match")).toBe('"3"');
  });

  it("shows a 412 (edited elsewhere) instead of overwriting and reloads the current version", async () => {
    const fetchMock = vi.fn(async () => json({ status: 412, code: "precondition_failed", detail: "version is 4, If-Match was 3" }, 412));
    vi.stubGlobal("fetch", fetchMock);
    render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: `Pause ${SUB.name}` }));
    expect(await screen.findByText(/changed by someone else/)).toBeInTheDocument();
    // Regression: without a reload every further click resent the stale If-Match and got 412 again.
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reloads the list when the subscription was deleted elsewhere (404)", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.stubGlobal("fetch", vi.fn(async () => json({ status: 404, code: "not_found", detail: "webhook subscription not found" }, 404)));
    render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: `Delete ${SUB.name}` }));
    expect(await screen.findByText(/no longer exists/)).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does not reload on other errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ status: 403, code: "forbidden", detail: "requires role merxa-admin" }, 403)));
    render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: `Pause ${SUB.name}` }));
    expect(await screen.findByText("requires role merxa-admin")).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("deletes a subscription after confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: `Delete ${SUB.name}` }));
    await waitFor(() => expect(refresh).toHaveBeenCalled());
    expect(calls(fetchMock)[0]?.[1].method).toBe("DELETE");
  });

  it("rejects a non-http URL locally and shows the signing secret once after creating", async () => {
    const fetchMock = vi.fn(async () =>
      json(
        {
          id: "s2",
          name: "BI",
          url: "https://bi.example.org/hook",
          events: ["sailing.updated"],
          active: true,
          created_at: "2026-10-06T10:00:00Z",
          version: 1,
          secret: "whsec_c2VjcmV0c2VjcmV0c2VjcmV0",
          secret_shown_once: true,
        },
        201,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<Webhooks subscriptions={[]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: /New subscription/ }));
    const form = screen.getByRole("form", { name: "New webhook subscription" });
    await userEvent.type(within(form).getByRole("textbox", { name: "Name" }), "BI");
    const url = within(form).getByRole("textbox", { name: "Endpoint URL" });
    await userEvent.type(url, "ftp://bi.example.org/hook");
    await userEvent.click(within(form).getByRole("button", { name: "Create subscription" }));
    // jsdom's type=url constraint validation may block submit; either way nothing is sent.
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.clear(url);
    await userEvent.type(url, "https://bi.example.org/hook");
    // every type but sailing.updated is unticked (M5 added the manual-change and lane types)
    for (const t of WEBHOOK_EVENT_TYPES.filter((e) => e !== "sailing.updated")) {
      await userEvent.click(within(form).getByRole("checkbox", { name: t }));
    }
    await userEvent.click(within(form).getByRole("button", { name: "Create subscription" }));
    expect(await screen.findByRole("textbox", { name: /signing secret of “BI”/ })).toHaveValue("whsec_c2VjcmV0c2VjcmV0c2VjcmV0");
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/webhook-subscriptions");
    expect(JSON.parse(String(init.body))).toEqual({ name: "BI", url: "https://bi.example.org/hook", events: ["sailing.updated"] });
    expect(header(init, "idempotency-key")).toBeTruthy();
  });

  it("explains a failed deliveries load", () => {
    render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="requires role merxa-admin" />);
    expect(screen.getByText(/Could not load deliveries: requires role merxa-admin/)).toBeInTheDocument();
  });
});

// ───────────────────────── production schedule ─────────────────────────
const SCHEDULE: FactorySchedule = {
  factory_code: "F-BD-01",
  from: "2026-09-28",
  to: "2026-10-11",
  lines: [
    {
      code: "FL-BD01-A",
      capabilities: ["outerwear", "padding"],
      std_minutes_day: 28800,
      version: 1,
      calendar: [
        { day: "2026-09-28", available_minutes: 28800, status: "open" },
        { day: "2026-10-02", available_minutes: 0, status: "closed" },
        { day: "2026-10-03", available_minutes: 14400, status: "reduced" },
        { day: "2026-10-05", available_minutes: 28800, status: "open" },
      ],
      orders: [
        { code: "MO-000001", po_line_ref: "PO-BD-0001#1", line_code: "FL-BD01-A", seq: 1, planned_start: "2026-09-20", planned_end: "2026-09-30", status: "done", hold_reason: null, version: 1 },
        { code: "MO-000002", po_line_ref: "PO-BD-0001#2", line_code: "FL-BD01-A", seq: 2, planned_start: "2026-10-04", planned_end: "2026-10-08", status: "held", hold_reason: "Trims late", version: 2 },
      ],
    },
    { code: "FL-BD01-B", capabilities: ["outerwear"], std_minutes_day: 28800, version: 1, calendar: [], orders: [] },
  ],
};

describe("ProductionSchedule", () => {
  it("renders a calendar strip per line with day status, today and week labels", () => {
    render(<ProductionSchedule schedule={SCHEDULE} today="2026-10-05" />);
    const strip = screen.getByRole("group", { name: /Calendar of FL-BD01-A/ });
    const days = strip.querySelectorAll("[data-day]");
    expect(days).toHaveLength(14);
    expect(strip.querySelector('[data-day="2026-10-02"]')).toHaveAttribute("data-status", "closed");
    expect(strip.querySelector('[data-day="2026-10-03"]')).toHaveAttribute("data-status", "reduced");
    expect(strip.querySelector('[data-day="2026-10-04"]')).toHaveAttribute("data-status", "unknown");
    expect(strip.querySelector('[aria-current="date"]')).toHaveAttribute("data-day", "2026-10-05");
    expect(within(strip).getByText("2026-W40")).toBeInTheDocument();
    expect(within(strip).getByText("2026-W41")).toBeInTheDocument();
  });

  it("draws production orders as bars linked to their PO line and flags held orders", () => {
    render(<ProductionSchedule schedule={SCHEDULE} today={null} />);
    const bar = document.querySelector('[data-order="MO-000001"]') as HTMLElement;
    expect(bar.tagName).toBe("A");
    expect(bar).toHaveAttribute("href", "/purchase-orders/PO-BD-0001?line=1");
    expect(bar.className).toContain("is-clipped-start");
    expect(bar).toHaveAttribute("data-cols", "1/4");
    const held = document.querySelector('[data-order="MO-000002"]') as HTMLElement;
    expect(held.className).toContain("mx-strip__bar--held");
    expect(screen.getByText("1 production order is on hold")).toBeInTheDocument();
    expect(screen.getByText(/Trims late/, { selector: "div" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /Calendar of FL-BD01-B/ })).toBeInTheDocument();
  });

  it("shows an empty state for a factory without lines", () => {
    render(<ProductionSchedule schedule={{ ...SCHEDULE, lines: [] }} today={null} />);
    expect(screen.getByText("No production lines")).toBeInTheDocument();
  });

  it("groups days into ISO-week segments", () => {
    expect(weekSegments(["2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"])).toEqual([
      { week: "2026-W40", start: 1, span: 2 },
      { week: "2026-W41", start: 3, span: 2 },
    ]);
  });
});

// ───────────────────────── lane capacity ─────────────────────────
describe("CapacityTable", () => {
  const lane: Lane = {
    code: "CGP-UK1-SA",
    origin: "BDCGP",
    dest: "UK1",
    mode: "sea_air",
    carrier_code: "EKSC",
    carrier_name: "Emirates SkyCargo",
    transit_days: 16,
    via: ["AEJEA"],
    alt_lane_code: "CGP-FXT-SEA",
    version: 1,
  };
  const rates: RateCard[] = [
    { lane_code: "CGP-UK1-SA", valid_from: "2026-07-01", valid_to: "2026-10-31", basis: "per_unit", rate_cents: 145, currency: "EUR", weekly_capacity: 10_000, version: 1 },
  ];
  const bookings: CapacityBooking[] = [
    { code: "CB-0001", lane_code: "CGP-UK1-SA", iso_week: "2026-W43", qty_units: 9_000, basis: "per_unit", status: "confirmed", expires_at: null, created_via: "ui", version: 1 },
    { code: "CB-0002", lane_code: "CGP-UK1-SA", iso_week: "2026-W44", qty_units: 10_000, basis: "per_unit", status: "held", expires_at: null, created_via: "ui", version: 1 },
  ];

  it("shows remaining of capacity per week with its utilisation state", () => {
    const weeks = ["2026-W42", "2026-W43", "2026-W44", "2026-W45"];
    render(<CapacityTable rows={capacityMatrix([lane], weeks, rates, bookings)} weeks={weeks} currentWeek="2026-W42" />);
    const table = screen.getByRole("table", { name: "Weekly lane capacity" });
    const cells = [...table.querySelectorAll("td[data-state]")].map((c) => c.getAttribute("data-state"));
    expect(cells).toEqual(["ok", "tight", "full", "none"]);
    expect(within(table).getByRole("columnheader", { name: /2026-W42/ })).toHaveAttribute("aria-current", "date");
    expect(table.querySelector('td[data-state="tight"]')).toHaveAttribute("title", expect.stringContaining("1,000 of 10,000 unit left"));
  });

  it("explains an empty window", () => {
    render(<CapacityTable rows={[]} weeks={["2027-W30"]} currentWeek={null} />);
    expect(screen.getByText("No lane capacity in this window")).toBeInTheDocument();
  });
});

describe("refresh and pinned-row regressions", () => {
  it("ApiClients picks up fresh server data after router.refresh() (last used, revoked elsewhere)", () => {
    const { rerender } = render(<ApiClients initialKeys={[KEY]} clients={[]} clientsError={null} />);
    expect(within(screen.getByRole("table", { name: "API keys" })).getByText("never")).toBeInTheDocument();
    const refreshed: ApiKey[] = [{ ...KEY, last_used_at: "2026-10-06T10:59:00Z", revoked_at: "2026-10-06T11:00:00Z", status: "revoked" }];
    rerender(<ApiClients initialKeys={refreshed} clients={[]} clientsError={null} />);
    const table = screen.getByRole("table", { name: "API keys" });
    expect(within(table).queryByText("never")).not.toBeInTheDocument();
    expect(within(table).getByText("Revoked")).toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: "Revoke Finance BI" })).not.toBeInTheDocument();
  });

  it("keeps the pinned totals row blank in formatted text columns", () => {
    const fmt = unlessPinned<unknown>(() => "—");
    expect(fmt({ node: { rowPinned: "bottom" } } as never)).toBe("");
    expect(fmt({ node: { rowPinned: null } } as never)).toBe("—");
  });
});

describe("ProductionSchedule overflow", () => {
  it("draws at most MAX_TRACKS bars per day and summarises the rest as +N", () => {
    const orders = Array.from({ length: MAX_TRACKS + 3 }, (_, i) => ({
      code: `MO-9${String(i).padStart(5, "0")}`,
      po_line_ref: `PO-VN-0001#${i + 1}`,
      line_code: "FL-VN01-A",
      seq: i + 1,
      planned_start: "2026-10-02",
      planned_end: "2026-10-02",
      status: "planned" as const,
      hold_reason: null,
      version: 1,
    }));
    const line = SCHEDULE.lines[0] as FactorySchedule["lines"][number];
    render(<ProductionSchedule schedule={{ ...SCHEDULE, lines: [{ ...line, orders }] }} today={null} />);
    expect(document.querySelectorAll("[data-order]")).toHaveLength(MAX_TRACKS);
    const more = document.querySelector('[data-more-day="2026-10-02"]');
    expect(more).toHaveTextContent("+3");
    expect(more).toHaveAttribute("title", expect.stringContaining("3 more orders"));
    // The full list is still available.
    expect(screen.getByText(`Production orders (${MAX_TRACKS + 3})`)).toBeInTheDocument();
  });
});

// ───────────────────────── review regressions (a11y, data story) ─────────────────────────
describe("accessibility regressions", () => {
  it("gives calendar days an image role so their aria-label is announced", () => {
    render(<ProductionSchedule schedule={SCHEDULE} today="2026-10-05" />);
    const strip = screen.getByRole("group", { name: /Calendar of FL-BD01-A/ });
    // A role-less div must not carry aria-label (axe: aria-prohibited-attr).
    for (const day of strip.querySelectorAll("[data-day]")) expect(day).toHaveAttribute("role", "img");
    expect(within(strip).getByRole("img", { name: /: closed, 0 min available/ })).toHaveAttribute("data-day", "2026-10-02");
  });

  it("states tight, full and missing capacity as text, not only as colour", () => {
    const lane: Lane = {
      code: "L1", origin: "BDCGP", dest: "UK1", mode: "sea_air", carrier_code: "C", carrier_name: "C", transit_days: 16, via: [], alt_lane_code: null, version: 1,
    };
    const rates: RateCard[] = [
      { lane_code: "L1", valid_from: "2026-07-01", valid_to: "2026-10-31", basis: "per_unit", rate_cents: 1, currency: "EUR", weekly_capacity: 100, version: 1 },
    ];
    const bookings: CapacityBooking[] = [
      { code: "B1", lane_code: "L1", iso_week: "2026-W43", qty_units: 90, basis: "per_unit", status: "held", expires_at: null, created_via: "ui", version: 1 },
      { code: "B2", lane_code: "L1", iso_week: "2026-W44", qty_units: 100, basis: "per_unit", status: "confirmed", expires_at: null, created_via: "ui", version: 1 },
    ];
    const weeks = ["2026-W42", "2026-W43", "2026-W44", "2026-W45"];
    render(<CapacityTable rows={capacityMatrix([lane], weeks, rates, bookings)} weeks={weeks} currentWeek={null} />);
    const table = screen.getByRole("table", { name: "Weekly lane capacity" });
    const text = (state: string) => table.querySelector(`td[data-state="${state}"]`)?.textContent ?? "";
    expect(text("ok")).not.toMatch(/booked|rate card/);
    expect(text("tight")).toContain("Tight (80% or more booked)");
    expect(text("full")).toContain("Fully booked");
    expect(text("none")).toContain("No rate card");
  });

  it("activates the row with Enter on a focused grid cell (keyboard equivalent of the row click)", async () => {
    type Row = { code: string; qty: number };
    const onRowClicked = vi.fn();
    render(
      <DataGrid<Row>
        label="Rows"
        rows={[{ code: "R-1", qty: 3 }]}
        columns={[{ field: "code" }, { field: "qty" }]}
        getRowId={(r) => r.code}
        onRowClicked={onRowClicked}
        layout="auto"
      />,
    );
    const cell = await waitFor(() => {
      const el = document.querySelector('.ag-row[row-id="R-1"] [col-id="qty"]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    act(() => cell.focus());
    fireEvent.keyDown(cell, { key: "ArrowLeft" });
    fireEvent.keyDown(cell, { key: "Enter", ctrlKey: true });
    expect(onRowClicked).not.toHaveBeenCalled();
    // AG Grid delivers cellKeyDown to grid-option listeners asynchronously.
    await new Promise((r) => setTimeout(r, 50));
    expect(onRowClicked).not.toHaveBeenCalled();
    fireEvent.keyDown(cell, { key: "Enter" });
    await waitFor(() => expect(onRowClicked).toHaveBeenCalledWith({ code: "R-1", qty: 3 }));
    expect(onRowClicked).toHaveBeenCalledTimes(1);
  });
});

describe("online orders at risk", () => {
  const order = (code: string, status: OnlineOrder["status"], reserved: number): OnlineOrder => ({
    code, market: "GB", channel: "online", store_code: null, promise_date: "2026-11-10", status, created_at: "2026-10-01T09:00:00Z", version: 1,
    lines: [{ line_no: 1, sku: "SKU-1", qty: 2, reserved_qty: reserved, version: 1 }], shortfall: 2 - reserved, at_risk: reserved < 2,
  });

  it("does not badge a cancelled order as at risk in the drawer", () => {
    render(<OnlineOrderGrid rows={[order("OO-2", "cancelled", 0)]} initialSelected="OO-2" />);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByText("At risk")).not.toBeInTheDocument();
  });

  it("badges an open order with unreserved units as at risk", () => {
    render(<OnlineOrderGrid rows={[order("OO-1", "open", 1)]} initialSelected="OO-1" />);
    expect(within(screen.getByRole("dialog")).getByText("At risk")).toBeInTheDocument();
  });
});

// ───────────────────────── independent review regressions ─────────────────────────
describe("review regressions: webhooks", () => {
  it("sends the version returned by a pause when resuming right away (no false 412)", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(async () => json({ ...SUB, active: false, version: 4 }))
      .mockImplementationOnce(async () => json({ ...SUB, active: true, version: 5 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: `Pause ${SUB.name}` }));
    // The router has not delivered fresh props yet: the row already reflects the write.
    await userEvent.click(await screen.findByRole("button", { name: `Resume ${SUB.name}` }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const [, second] = calls(fetchMock)[1] as FetchCall;
    expect(JSON.parse(String(second.body))).toEqual({ active: true });
    expect(header(second, "if-match")).toBe('"4"');
    expect(screen.queryByText(/changed by someone else/)).not.toBeInTheDocument();
  });

  it("drops local overrides when the reloaded server list arrives", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ ...SUB, active: false, version: 4 })));
    const { rerender } = render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: `Pause ${SUB.name}` }));
    expect(await screen.findByRole("button", { name: `Resume ${SUB.name}` })).toBeInTheDocument();
    // Someone resumed it meanwhile: the server list wins.
    rerender(<Webhooks subscriptions={[{ ...SUB, active: true, version: 5 }]} deliveries={[]} deliveriesError="x" />);
    expect(screen.getByRole("button", { name: `Pause ${SUB.name}` })).toBeInTheDocument();
  });

  it("removes a deleted subscription from the list at once", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    render(<Webhooks subscriptions={[SUB]} deliveries={[]} deliveriesError="x" />);
    await userEvent.click(screen.getByRole("button", { name: `Delete ${SUB.name}` }));
    await waitFor(() => expect(screen.queryByRole("button", { name: `Delete ${SUB.name}` })).not.toBeInTheDocument());
    expect(screen.getByText("No webhook subscriptions")).toBeInTheDocument();
  });

  it("does not leave a password from the endpoint URL on screen", () => {
    render(<Webhooks subscriptions={[{ ...SUB, url: "https://svc:hunter2@hooks.example.org/in" }]} deliveries={[]} deliveriesError={null} />);
    const table = screen.getByRole("table", { name: "Webhook subscriptions" });
    expect(within(table).getByText("https://svc:••••••@hooks.example.org/in")).toBeInTheDocument();
    expect(table.textContent).not.toContain("hunter2");
  });
});

describe("review regressions: capacity", () => {
  it("does not present rate-card capacity as remaining when the bookings could not be loaded", () => {
    const lane: Lane = {
      code: "CGP-UK1-SA",
      origin: "BDCGP",
      dest: "UK1",
      mode: "sea_air",
      carrier_code: "EKSC",
      carrier_name: "Emirates SkyCargo",
      transit_days: 16,
      via: ["AEJEA"],
      alt_lane_code: null,
      version: 1,
    };
    const rates: RateCard[] = [
      { lane_code: lane.code, valid_from: null, valid_to: null, basis: "per_unit", rate_cents: 145, currency: "EUR", weekly_capacity: 10_000, version: 1 },
    ];
    const weeks = ["2026-W42"];
    render(<CapacityTable rows={capacityMatrix([lane], weeks, rates, [])} weeks={weeks} currentWeek={null} bookingsUnavailable />);
    expect(screen.queryByRole("table", { name: "Weekly lane capacity" })).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("Remaining lane capacity cannot be shown");
    expect(screen.queryByText("10,000")).not.toBeInTheDocument();
  });
});

describe("review regressions: supplier portal", () => {
  const msg = (id: string, status: "new" | "read", version = 1): SupplierMessage => ({
    id,
    supplier_code: "TRIM-BD-01",
    received_at: "2026-10-06T08:00:00Z",
    subject: `Subject ${id}`,
    body: "Trims delayed 7 days.",
    status,
    version,
  });

  it("offers Mark read for the message shown first (it is not marked read automatically)", async () => {
    const fetchMock = vi.fn(async () => json({ ...msg("m1", "read"), version: 2 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<SupplierInbox messages={[msg("m1", "new")]} supplierNames={{}} canMarkRead initialSelected={null} />);
    expect(screen.getByTestId("inbox-count")).toHaveTextContent("1 message · 1 unread");
    await userEvent.click(screen.getByRole("button", { name: "Mark read" }));
    await waitFor(() => expect(screen.getByTestId("inbox-count")).toHaveTextContent("1 message · 0 unread"));
    const [path, init] = calls(fetchMock)[0] as FetchCall;
    expect(path).toBe("/v1/supplier-messages/m1");
    expect(JSON.parse(String(init.body))).toEqual({ status: "read" });
    expect(header(init, "if-match")).toBe('"1"');
    expect(screen.getByRole("button", { name: "Mark unread" })).toBeInTheDocument();
  });

  it("hides the status controls without changes:preview", () => {
    render(<SupplierInbox messages={[msg("m1", "new")]} supplierNames={{}} canMarkRead={false} initialSelected={null} />);
    expect(screen.queryByRole("button", { name: "Mark read" })).not.toBeInTheDocument();
  });

  it("reloads after a 412 and then sends the current version", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(async () => json({ status: 412, code: "precondition_failed", detail: "version is 2, If-Match was 1" }, 412))
      .mockImplementationOnce(async () => json({ ...msg("m1", "read"), version: 3 }));
    vi.stubGlobal("fetch", fetchMock);
    const { rerender } = render(<SupplierInbox messages={[msg("m1", "new")]} supplierNames={{}} canMarkRead initialSelected={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Mark read" }));
    expect(await screen.findByText(/changed elsewhere/)).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledTimes(1);
    // The reloaded server list carries version 2.
    rerender(<SupplierInbox messages={[msg("m1", "new", 2)]} supplierNames={{}} canMarkRead initialSelected={null} />);
    await userEvent.click(screen.getByRole("button", { name: "Mark read" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(header((calls(fetchMock)[1] as FetchCall)[1], "if-match")).toBe('"2"');
  });
});
