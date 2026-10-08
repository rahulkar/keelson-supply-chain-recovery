import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fakeApi, jsonResponse, makeMe, withSession } from "../../../test/session";
import { AppShell } from "./AppShell";
import { PersonaSwitcher } from "./PersonaSwitcher";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/" }));

const PERSONAS = [
  { username: "priya", name: "Priya Shah", role_label: "Planner", initials: "PS" },
  { username: "marco", name: "Marco Rossi", role_label: "Planner · Approver", initials: "MR" },
];

describe("PersonaSwitcher (DEMO)", () => {
  it("lists personas, switches with POST /api/demo/login-as and reloads", async () => {
    const user = userEvent.setup();
    const reload = vi.fn();
    const { api, calls } = fakeApi([
      (url) => (url === "/api/demo/personas" ? jsonResponse(200, PERSONAS) : undefined),
      (url) => (url === "/api/demo/login-as" ? jsonResponse(200, { ok: true }) : undefined),
    ]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <PersonaSwitcher reload={reload} />
      </Wrapper>,
    );
    await user.click(screen.getByRole("button", { name: /demo persona: priya shah/i }));
    const marco = await screen.findByRole("menuitem", { name: /Marco Rossi/ });
    expect(screen.getByRole("menuitem", { name: /Priya Shah/ })).toHaveAttribute("aria-current", "true");
    await user.click(marco);
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    const login = calls.find((c) => c.url === "/api/demo/login-as")!;
    expect(login.method).toBe("POST");
    expect(login.body).toEqual({ username: "marco" });
    expect(login.headers["X-CSRF-Token"]).toBe("csrf-test");
  });
});

describe("AppShell", () => {
  it("shows the brand, history, persona switcher (demo) and the user menu", () => {
    render(
      <AppShell me={makeMe()}>
        <p>content</p>
      </AppShell>,
    );
    expect(screen.getByRole("link", { name: /Keelson/ })).toHaveAttribute("href", "/");
    expect(screen.getByRole("button", { name: "History" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /demo persona/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Account: Priya Shah" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveTextContent("content");
  });

  it("hides the persona switcher outside demo mode and history for roles without cases", () => {
    render(
      <AppShell me={makeMe({ roles: ["platform-admin"] }, false)}>
        <p>content</p>
      </AppShell>,
    );
    expect(screen.queryByRole("button", { name: /demo persona/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "History" })).not.toBeInTheDocument();
  });

  it("opens the history drawer with recent cases", async () => {
    const user = userEvent.setup();
    const { api } = fakeApi([
      (url) =>
        url === "/api/cases?limit=20"
          ? jsonResponse(200, {
              items: [
                { id: "c1", title: "Trim delay", mode: "actual", status: "awaiting_approval", updated_at: "2026-10-05T09:12:00Z", last_message_preview: "Approve 1 changes" },
              ],
            })
          : undefined,
    ]);
    const { HistoryDrawer } = await import("./HistoryDrawer");
    render(<HistoryDrawer />, { wrapper: withSession(makeMe(), api) });
    await user.click(screen.getByRole("button", { name: "History" }));
    const link = await screen.findByRole("link", { name: /Trim delay/ });
    expect(link).toHaveAttribute("href", "/cases/c1");
    expect(screen.getByText("Awaiting approval")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All cases" })).toHaveAttribute("href", "/cases");
  });
});
