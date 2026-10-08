import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChangeSetSummary } from "@/lib/types";
import { ExternalChangesBanner } from "./ExternalChangesBanner";
import { PersonaSwitcher } from "./PersonaSwitcher";
import { isActive } from "./Sidebar";
import { initials, roleLabels } from "./UserMenu";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/change-batches",
}));

const NOW = Date.parse("2026-10-06T12:00:00Z");

function cs(code: string, minutesAgo: number | null, extra: Partial<ChangeSetSummary> = {}): ChangeSetSummary {
  return {
    change_set_id: `id-${code}`,
    code,
    title: `Rebalance ${code}`,
    status: minutesAgo === null ? "validated" : "applied",
    items_count: 2,
    preview_hash: null,
    approval_ref: "APR-0001",
    approved_by: "user:marco",
    initiated_by: "Priya Shah",
    applied_by_display: "Marco Rossi",
    applied_by_username: "marco.rossi",
    via: "Keelson",
    created_at: new Date(NOW - 3_600_000).toISOString(),
    validated_at: null,
    applied_at: minutesAgo === null ? null : new Date(NOW - minutesAgo * 60_000).toISOString(),
    version: 3,
    attribution_text: minutesAgo === null ? null : "Changed by Marco Rossi via Keelson (APR-0001)",
    ...extra,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("ExternalChangesBanner", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
    window.sessionStorage.clear();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    refresh.mockReset();
  });

  it("announces a batch applied in the last 10 minutes", () => {
    render(<ExternalChangesBanner initial={[cs("CS-0003", 3), cs("CS-0001", 45)]} />);
    const banner = screen.getByTestId("external-changes-banner");
    expect(banner).toHaveTextContent("External changes received");
    expect(banner).toHaveTextContent("Changed by Marco Rossi via Keelson (APR-0001)");
    expect(banner).toHaveTextContent("applied 3 min ago");
    expect(screen.getByRole("link", { name: "CS-0003" })).toHaveAttribute("href", "/change-batches/CS-0003");
    expect(screen.queryByRole("link", { name: "CS-0001" })).not.toBeInTheDocument();
  });

  it("stays hidden when nothing recent was applied", () => {
    render(<ExternalChangesBanner initial={[cs("CS-0001", 45), cs("CS-0002", null)]} />);
    expect(screen.queryByTestId("external-changes-banner")).not.toBeInTheDocument();
  });

  it("polls the change-set list and offers a data reload for new batches", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [cs("CS-0004", 0), cs("CS-0003", 3)], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ExternalChangesBanner initial={[cs("CS-0003", 3)]} pollMs={1000} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(fetchMock).toHaveBeenCalledWith("/v1/change-sets?status=applied&limit=25", expect.objectContaining({ credentials: "same-origin" }));
    await waitFor(() => expect(screen.getByTestId("external-changes-banner")).toHaveTextContent("2 change batches"));
    await userEvent.click(screen.getByRole("button", { name: /reload data/i }));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("can be dismissed for the session", async () => {
    render(<ExternalChangesBanner initial={[cs("CS-0003", 3)]} />);
    await userEvent.click(screen.getByRole("button", { name: "Dismiss notice" }));
    expect(screen.queryByTestId("external-changes-banner")).not.toBeInTheDocument();
    expect(JSON.parse(window.sessionStorage.getItem("merxa.dismissedExternalChanges") ?? "[]")).toEqual(["CS-0003"]);
  });
});

describe("PersonaSwitcher", () => {
  afterEach(() => vi.unstubAllGlobals());

  const personas = [
    { username: "priya.shah", name: "Priya Shah", role_label: "Merchandise Planner", initials: "PS" },
    { username: "marco.rossi", name: "Marco Rossi", role_label: "Operations Director", initials: "MR" },
  ];

  it("logs in as the chosen persona and navigates to the target", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ user: { username: "marco.rossi" }, demo_mode: true }));
    vi.stubGlobal("fetch", fetchMock);
    const navigate = vi.fn();
    render(<PersonaSwitcher personas={personas} next="/purchase-orders" currentUsername={null} navigate={navigate} />);
    await userEvent.click(screen.getByRole("button", { name: /Continue as Marco Rossi/ }));
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/auth/demo/login-as",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ username: "marco.rossi" }), credentials: "same-origin" }),
    );
    expect(navigate).toHaveBeenCalledWith("/purchase-orders");
  });

  it("shows the API problem when the login fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ status: 503, code: "not_configured", detail: "DEMO_USER_PASSWORD is not configured" }, 503)),
    );
    const navigate = vi.fn();
    render(<PersonaSwitcher personas={personas} next="/" currentUsername="priya.shah" navigate={navigate} />);
    expect(screen.getByRole("button", { name: /Continue as Priya Shah/ })).toHaveAttribute("aria-current", "true");
    await userEvent.click(screen.getByRole("button", { name: /Continue as Priya Shah/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("DEMO_USER_PASSWORD is not configured");
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe("shell helpers", () => {
  it("marks nested routes active", () => {
    expect(isActive("/change-batches/CS-0001", "/change-batches")).toBe(true);
    expect(isActive("/change-batches", "/change-batches")).toBe(true);
    expect(isActive("/change-batchesX", "/change-batches")).toBe(false);
  });

  it("labels product roles and initials", () => {
    expect(roleLabels(["default-roles-merxa", "ops-director", "offline_access"])).toEqual(["Operations Director"]);
    expect(initials("Marco Rossi", "marco.rossi")).toBe("MR");
    expect(initials(null, "merxa.admin")).toBe("MA");
    expect(initials("Cher", "cher")).toBe("CH");
  });
});
