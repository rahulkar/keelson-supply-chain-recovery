import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fakeApi, makeMe, withSession } from "./session";

const session = vi.hoisted(() => ({ requireMe: vi.fn() }));
vi.mock("@/lib/server/session", () => session);
vi.mock("next/navigation", () => ({
  usePathname: () => "/admin",
  useRouter: () => ({ push: vi.fn() }),
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import AdminLayout from "@/app/(app)/admin/layout";
import AdminConnectorRoute from "@/app/(app)/admin/connectors/[slug]/page";
import AdminTenantsRoute from "@/app/(app)/admin/tenants/page";

beforeEach(() => session.requireMe.mockReset());

function renderWith(me: ReturnType<typeof makeMe>, el: ReactNode) {
  const { api } = fakeApi([]);
  const Wrapper = withSession(me, api);
  return render(<Wrapper>{el}</Wrapper>);
}

describe("admin route gates (role-gated: tenant-admin / platform-admin)", () => {
  it("a planner sees 'Admins only' and never the admin content", async () => {
    const me = makeMe({ roles: ["planner", "approver"] });
    session.requireMe.mockResolvedValue(me);
    renderWith(me, await AdminLayout({ children: <p>secret admin content</p> }));
    expect(screen.getByRole("heading", { level: 1, name: "Admins only" })).toBeInTheDocument();
    expect(screen.queryByText("secret admin content")).toBeNull();
  });

  it("a tenant admin gets the admin shell", async () => {
    const me = makeMe({ roles: ["tenant-admin"] });
    session.requireMe.mockResolvedValue(me);
    renderWith(me, await AdminLayout({ children: <p>admin content</p> }));
    expect(screen.getByRole("navigation", { name: "Admin" })).toBeInTheDocument();
    expect(screen.getByText("admin content")).toBeInTheDocument();
  });

  it("Tenants is platform-admin only", async () => {
    const sam = makeMe({ roles: ["tenant-admin"] });
    session.requireMe.mockResolvedValue(sam);
    const { unmount } = renderWith(sam, await AdminTenantsRoute());
    expect(screen.getByRole("heading", { level: 1, name: "Platform admins only" })).toBeInTheDocument();
    unmount();
    const ava = makeMe({ roles: ["platform-admin"] });
    session.requireMe.mockResolvedValue(ava);
    renderWith(ava, await AdminTenantsRoute());
    expect(screen.getByRole("heading", { level: 1, name: "Tenants" })).toBeInTheDocument();
  });

  it("connector detail rejects malformed slugs", async () => {
    await expect(AdminConnectorRoute({ params: Promise.resolve({ slug: "../etc" }) })).rejects.toThrow("NEXT_NOT_FOUND");
    await expect(AdminConnectorRoute({ params: Promise.resolve({ slug: "merxa" }) })).resolves.toBeTruthy();
  });
});
