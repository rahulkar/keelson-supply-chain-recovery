import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/http";
import type { ChangeSetItem, SessionUser, SupplierMessage } from "@/lib/types";
import { entityHref } from "./change-batches/AuditTable";
import { ItemDiffTable } from "./change-batches/ItemDiffTable";
import { idempotencyKeyFor, isRetryable } from "./demo/DemoDirector";
import { Sidebar } from "./shell/Sidebar";
import { UserMenu } from "./shell/UserMenu";
import { SupplierInbox } from "./supplier/SupplierInbox";
import { Drawer } from "./ui/Drawer";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/inventory",
}));

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("Sidebar", () => {
  it("names every module link (the narrow icon rail hides labels only visually)", () => {
    render(<Sidebar showDemoDirector={false} />);
    const nav = screen.getByRole("navigation", { name: "Modules" });
    for (const name of ["Purchase Orders", "Supplier Portal", "Inventory", "Transfer Orders", "DCs & Stores", "Change Batches", "Audit Log"]) {
      expect(within(nav).getByRole("link", { name })).toHaveAttribute("title", name);
    }
    expect(within(nav).getByRole("link", { name: "Inventory" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).queryByRole("link", { name: "Demo Director" })).not.toBeInTheDocument();
    for (const svg of nav.querySelectorAll("svg")) expect(svg).toHaveAttribute("aria-hidden", "true");
  });
});

describe("Drawer", () => {
  it("is a labelled modal dialog that traps Tab and closes on Escape", async () => {
    const onClose = vi.fn();
    render(
      <>
        <button type="button">outside</button>
        <Drawer title="TO-0001" onClose={onClose}>
          <a href="/x">first link</a>
          <button type="button">last button</button>
        </Drawer>
      </>,
    );
    const dialog = screen.getByRole("dialog", { name: "TO-0001" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("link", { name: "first link" })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "last button" })).toHaveFocus();
    await userEvent.tab(); // wraps instead of escaping to "outside"
    expect(close).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByRole("button", { name: "last button" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("UserMenu", () => {
  const user: SessionUser = {
    sub: "s",
    username: "marco.rossi",
    name: "Marco Rossi",
    email: null,
    roles: ["ops-director", "default-roles-merxa"],
    scopes: [],
  };

  it("is a disclosure (no incomplete ARIA menu) and returns focus on Escape", async () => {
    render(<UserMenu user={user} demoMode />);
    const toggle = screen.getByRole("button", { name: /Marco Rossi/ });
    expect(toggle).not.toHaveAttribute("aria-haspopup");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const panel = screen.getByRole("group", { name: "Account" });
    expect(toggle).toHaveAttribute("aria-controls", panel.id);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByText("Operations Director")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: "Account" })).not.toBeInTheDocument();
    expect(toggle).toHaveFocus();
  });

  it("does not follow a non-http logout URL from the API", async () => {
    const assign = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => json({ status: "logged_out", logout_url: "javascript:alert(1)" })));
    render(<UserMenu user={user} demoMode={false} navigate={assign} />);
    await userEvent.click(screen.getByRole("button", { name: /Marco Rossi/ }));
    await userEvent.click(screen.getByRole("button", { name: /Sign out/ }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
  });
});

describe("SupplierInbox", () => {
  const msg = (id: string, status: "new" | "read", body = "Trims delayed 7 days."): SupplierMessage => ({
    id,
    supplier_code: "TRIM-BD-01",
    received_at: "2026-10-06T08:00:00Z",
    subject: `Subject ${id}`,
    body,
    status,
    version: 1,
  });

  it("renders bodies as plain text and keeps the unread count live when a message is opened", async () => {
    const fetchMock = vi.fn(async () => json({ ...msg("m2", "read"), version: 2 }));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <SupplierInbox
        messages={[msg("m1", "read", '<img src=x onerror="alert(1)"> **bold**'), msg("m2", "new")]}
        supplierNames={{ "TRIM-BD-01": "Meghna Trims Ltd" }}
        canMarkRead
        initialSelected={null}
      />,
    );
    expect(screen.getByText('<img src=x onerror="alert(1)"> **bold**')).toBeInTheDocument();
    expect(document.querySelector(".mx-message-body img")).toBeNull();
    expect(screen.getByTestId("inbox-count")).toHaveTextContent("2 messages · 1 unread");
    await userEvent.click(screen.getByRole("button", { name: /Subject m2/ }));
    await waitFor(() => expect(screen.getByTestId("inbox-count")).toHaveTextContent("2 messages · 0 unread"));
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/supplier-messages/m2",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ status: "read" }), headers: expect.objectContaining({ "if-match": '"1"' }) }),
    );
  });
});

describe("ItemDiffTable", () => {
  it("explains missing previews of items that failed validation", () => {
    const item: ChangeSetItem = {
      seq: 1,
      op: "po_line.reschedule",
      target_ref: "PO-BD-0001#1",
      expected_version: 1,
      payload: { eta_dc: "2026-11-05" },
      before: null,
      after: null,
      status: "error",
      error: "version_conflict",
    };
    render(<ItemDiffTable items={[item]} />);
    expect(screen.getByText("No before/after values were recorded for this item (it failed validation or apply).")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("version_conflict");
  });
});

describe("audit entity links", () => {
  it("encodes every query value", () => {
    expect(entityHref({ entity_type: "inventory_position", entity_ref: "dc/UK1/PUF-W-BLK-M" })).toBe(
      "/inventory?location_type=dc&location=UK1&sku=PUF-W-BLK-M",
    );
    expect(entityHref({ entity_type: "inventory_position", entity_ref: "x&y=1/A B/C#D" })).toBe(
      "/inventory?location_type=x%26y%3D1&location=A+B&sku=C%23D",
    );
    expect(entityHref({ entity_type: "transfer_order", entity_ref: "TO-0001" })).toBe("/transfer-orders?code=TO-0001");
    expect(entityHref({ entity_type: "po_line", entity_ref: "PO-BD-0001#2" })).toBe("/purchase-orders/PO-BD-0001");
  });
});

describe("demo director retries", () => {
  it("reuses the Idempotency-Key only for an identical retry after an ambiguous failure", () => {
    const pending = { path: "/v1/admin/scenario:inject", body: JSON.stringify({ kind: "trim_delay", payload: {} }), key: "k-1" };
    expect(idempotencyKeyFor(pending, "/v1/admin/scenario:inject", { kind: "trim_delay", payload: {} })).toBe("k-1");
    expect(idempotencyKeyFor(pending, "/v1/admin/scenario:reset", {})).not.toBe("k-1");
    expect(idempotencyKeyFor(pending, "/v1/admin/scenario:inject", { kind: "po_eta_change", payload: {} })).not.toBe("k-1");
    expect(idempotencyKeyFor(null, "/v1/admin/scenario:reset", {})).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("treats network errors, 5xx and in-flight conflicts as ambiguous", () => {
    expect(isRetryable(new ApiError(0, { code: "network_error" }))).toBe(true);
    expect(isRetryable(new ApiError(503, {}))).toBe(true);
    expect(isRetryable(new ApiError(409, { code: "idempotency_in_flight" }))).toBe(true);
    expect(isRetryable(new ApiError(403, { code: "forbidden" }))).toBe(false);
    expect(isRetryable(new ApiError(400, { code: "invalid_parameter" }))).toBe(false);
    expect(isRetryable(new Error("x"))).toBe(false);
  });
});
