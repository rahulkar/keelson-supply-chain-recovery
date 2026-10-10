import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AdminShell } from "@/components/admin/AdminShell";
import { AgentsPage } from "@/components/admin/AgentsPage";
import { expectNoA11yViolations } from "./a11y";
import { adminRoutes, makeAdminServer, type AdminServer } from "./adminMock";
import { fakeApi, makeMe, withSession } from "./session";

// M8 (docs/contracts/125-m8-optimizer-cpsat.md §7): Admin → Agents "Planning solver".
const nav = vi.hoisted(() => ({ pathname: "/admin/agents", push: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ push: nav.push, refresh: vi.fn() }) }));

vi.setConfig({ testTimeout: 20_000 });

const SAM = () => makeMe({ username: "sam", name: "Sam Okafor", roles: ["tenant-admin", "planner"] });
const AVA = () => makeMe({ username: "ava", name: "Ava Lindqvist", roles: ["platform-admin"] });

let server: AdminServer;
beforeEach(() => {
  server = makeAdminServer();
  nav.push.mockReset();
});

function renderAdmin(ui: ReactNode, me = SAM()) {
  const { api, calls } = fakeApi(adminRoutes(server));
  const Wrapper = withSession(me, api);
  const utils = render(
    <Wrapper>
      <main>
        <AdminShell>{ui}</AdminShell>
      </main>
    </Wrapper>,
  );
  return { ...utils, calls };
}

describe("Agents: Planning solver (M8)", () => {
  it("saves the integer stage and the sourcing terms through a merge patch", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderAdmin(<AgentsPage />);
    const card = await screen.findByRole("region", { name: "Planning solver" });
    const stage = within(card).getByLabelText("Whole units");
    expect(stage).toHaveDisplayValue("LP rounding (default)");
    await user.selectOptions(stage, "cpsat");
    await user.type(within(card).getByLabelText("Case pack (units)"), "12");
    await user.selectOptions(within(card).getByLabelText("Minimum order counts"), "style");
    await user.click(within(card).getByRole("checkbox", { name: "One capacity per factory (shared by its lanes)" }));
    expect(screen.getByText("Unsaved changes: planning.")).toBeInTheDocument();
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: "Review and save" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Save a new version?" })).getByRole("button", { name: "Save version" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({
      base_version: "v3",
      spec_patch: {
        planning: {
          solver_integer_stage: "cpsat",
          alt_sourcing: { order_multiple_units: 12, moq_basis: "style", share_factory_capacity: true },
        },
      },
    });
  });

  it("a case pack below 1 blocks saving", async () => {
    const user = userEvent.setup();
    renderAdmin(<AgentsPage />);
    const card = await screen.findByRole("region", { name: "Planning solver" });
    const pack = within(card).getByLabelText("Case pack (units)");
    await user.type(pack, "0");
    expect(await within(card).findByText("A whole number from 1 to 100,000.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review and save" })).toHaveAttribute("aria-disabled", "true");
  });

  it("tenant admin: removing a blocked factory needs a second admin; adding one does not", async () => {
    const user = userEvent.setup();
    server.spec.spec.planning = { default_delay_days: 7, alt_sourcing: { blocked_factories: ["F-MA-01"] } } as typeof server.spec.spec.planning;
    renderAdmin(<AgentsPage />);
    const card = await screen.findByRole("region", { name: "Planning solver" });
    const blocked = within(card).getByLabelText("Blocked factories (compliance)");
    expect(blocked).toHaveValue("F-MA-01");
    expect(blocked).toHaveAccessibleDescription(/Removing a factory needs a second admin/);
    await user.type(blocked, ", F-TR-01");
    expect(document.querySelector("[data-second-admin]")).toBeNull();
    await user.clear(blocked);
    await user.type(blocked, "F-TR-01");
    const banner = await screen.findByRole("note");
    expect(banner).toHaveTextContent("Compliance block: removing F-MA-01 lets the planner buy from it");
    expect(screen.getByRole("button", { name: "Review and request approval" })).toBeInTheDocument();
  });

  it("platform admin: no lock and no second-admin banner", async () => {
    const user = userEvent.setup();
    server.spec.spec.planning = { default_delay_days: 7, alt_sourcing: { blocked_factories: ["F-MA-01"] } } as typeof server.spec.spec.planning;
    renderAdmin(<AgentsPage />, AVA());
    const blocked = await screen.findByLabelText("Blocked factories (compliance)");
    expect(blocked).not.toHaveAccessibleDescription(/second admin/);
    await user.clear(blocked);
    expect(document.querySelector("[data-second-admin]")).toBeNull();
    expect(screen.getByRole("button", { name: "Review and save" })).toBeInTheDocument();
  });
});
