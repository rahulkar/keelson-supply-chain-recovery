import { toast } from "@keelson/ui";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AdminShell } from "@/components/admin/AdminShell";
import { AgentsPage } from "@/components/admin/AgentsPage";
import { AuditPage, filterAudit } from "@/components/admin/AuditPage";
import { ConnectorDetail } from "@/components/admin/ConnectorDetail";
import { ConnectorWizard } from "@/components/admin/ConnectorWizard";
import { ConnectorsPage } from "@/components/admin/ConnectorsPage";
import { GatewayPage, aliasError, parseParams, summariseUsage } from "@/components/admin/GatewayPage";
import { OverviewPage } from "@/components/admin/OverviewPage";
import { TenantsPage, parseUsernames } from "@/components/admin/TenantsPage";
import { auditListSchema, usageSchema } from "@/lib/admin/types";
import { expectNoA11yViolations } from "./a11y";
import { adminRoutes, makeAdminServer, type AdminServer } from "./adminMock";
import { fakeApi, makeMe, withSession } from "./session";

const nav = vi.hoisted(() => ({ pathname: "/admin", push: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ push: nav.push, refresh: vi.fn() }) }));

// Page renders + axe runs: generous per-test budget (CI and Docker builds share CPU).
vi.setConfig({ testTimeout: 20_000 });

const SAM = () => makeMe({ username: "sam", name: "Sam Okafor", roles: ["tenant-admin", "planner"] });
const AVA = () => makeMe({ username: "ava", name: "Ava Lindqvist", roles: ["platform-admin"] });

let server: AdminServer;
beforeEach(() => {
  server = makeAdminServer();
  nav.pathname = "/admin";
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

const settle = () => act(async () => {});

describe("admin shell", () => {
  it("shows Tenants only to platform admins and marks the current page", async () => {
    nav.pathname = "/admin/connectors/merxa";
    const { unmount } = renderAdmin(<p>x</p>);
    const navEl = screen.getByRole("navigation", { name: "Admin" });
    expect(within(navEl).queryByRole("link", { name: "Tenants" })).toBeNull();
    expect(within(navEl).getByRole("link", { name: "Connectors" })).toHaveAttribute("aria-current", "page");
    expect(within(navEl).getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");
    unmount();
    renderAdmin(<p>x</p>, AVA());
    expect(screen.getByRole("link", { name: "Tenants" })).toBeInTheDocument();
  });
});

describe("Overview", () => {
  it("shows the active spec version, connector status and budget used", async () => {
    const { container } = renderAdmin(<OverviewPage />);
    expect(await screen.findByText("v3")).toBeInTheDocument();
    expect(await screen.findByText("Merxa Retail Suite")).toBeInTheDocument();
    expect(screen.getByText("1 to review")).toBeInTheDocument();
    const meter = await screen.findByRole("meter", { name: "Budget used" });
    expect(meter).toHaveAttribute("aria-valuetext", "€42.50 of €50.00 per 30d");
    await expectNoA11yViolations(container);
  });
});

describe("AI Gateway", () => {
  it("tenant admin: models with capability badges and health, no provider models, budget locked", async () => {
    const { container } = renderAdmin(<GatewayPage />);
    const table = await screen.findByRole("table", { name: "Model aliases" });
    expect(within(table).getByRole("rowheader", { name: "fast-default" })).toBeInTheDocument();
    expect(within(table).queryByText("Provider model")).toBeNull();
    expect(within(table).getByText("Unhealthy")).toBeInTheDocument();
    expect(within(table).getAllByText(/: not supported/).length).toBe(1);
    expect(screen.queryByRole("link", { name: /Open advanced console/ })).toBeNull();
    const budget = await screen.findByLabelText(/Budget \(€\)/);
    expect(budget).toBeDisabled();
    expect(budget).toHaveAccessibleDescription(/Requires platform admin/);
    expect(await screen.findByRole("table", { name: "Spend per day" })).toBeInTheDocument();
    // The engine's key prefix already ends in an ellipsis: shown once.
    expect(screen.getByText("sk-abcd…")).toBeInTheDocument();
    expect(screen.queryByText(/……/)).toBeNull();
    await expectNoA11yViolations(container);
  });

  it("saves RPM/TPM with PATCH and rotates the key after confirmation", async () => {
    const user = userEvent.setup();
    const { calls } = renderAdmin(<GatewayPage />);
    const rpm = await screen.findByLabelText("Requests per minute");
    await user.clear(rpm);
    await user.type(rpm, "60");
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ rpm_limit: 60 });

    await user.click(screen.getByRole("button", { name: "Rotate key" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Rotate the gateway key?" });
    expect(calls.some((c) => c.url.endsWith("/rotate-key"))).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Rotate key" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/gateway/tenant/rotate-key" && c.method === "POST")).toBe(true));
  });

  it("platform admin: provider models, console link, add-model validation", async () => {
    const user = userEvent.setup();
    const { calls } = renderAdmin(<GatewayPage />, AVA());
    const table = await screen.findByRole("table", { name: "Model aliases" });
    expect(within(table).getByText("vertex_ai/gemini-2.5-flash")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open advanced console/ })).toHaveAttribute("href", "/api/console/handoff?target=gateway");
    await user.click(screen.getByRole("button", { name: "Add model" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a model alias" });
    await user.type(within(dialog).getByLabelText(/^Alias/), "gemini/flash");
    await user.click(within(dialog).getByRole("button", { name: "Add model" }));
    expect(within(dialog).getByText(/never start with a provider prefix/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("helpers: alias rules, params JSON, usage summary", () => {
    expect(aliasError("fast-default")).toBeNull();
    expect(aliasError("anthropic/claude-fast")).toBeNull();
    expect(aliasError("openai/gpt")).toMatch(/provider prefix/);
    expect(aliasError("Bad_Alias")).toMatch(/Lower-case/);
    expect(parseParams('{"temperature":0.2}')).toEqual({ ok: true, value: { temperature: 0.2 } });
    expect(parseParams("[1]").ok).toBe(false);
    const { days, aliases } = summariseUsage(usageSchema.parse(makeAdminServer().usage));
    expect(days).toEqual([
      { date: "2026-10-04", spend: 0.5 },
      { date: "2026-10-05", spend: 2 },
    ]);
    expect(aliases[0]).toMatchObject({ alias: "fast-default", spend: 1.75, requests: 13 });
  });
});

describe("Agents", () => {
  it("tenant admin (M7 four-eyes): raising a guarded field needs a second admin; lowering saves a new version", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderAdmin(<AgentsPage />);
    const lines = await screen.findByLabelText(/Auto-apply limit \(lines\)/);
    expect(lines).toHaveAccessibleDescription(/Raising it needs a second admin/);
    await user.clear(lines);
    await user.type(lines, "12");
    // M7: no longer blocked — saving creates a change request another admin approves.
    const banner = await screen.findByRole("note");
    expect(banner).toHaveTextContent("Requires a second admin");
    expect(banner).toHaveTextContent("raising it above 10");
    expect(screen.getByRole("button", { name: "Review and request approval" })).not.toHaveAttribute("aria-disabled", "true");

    await user.clear(lines);
    await user.type(lines, "5");
    expect(document.querySelector("[data-second-admin]")).toBeNull();
    expect(screen.getByText("Unsaved changes: policy.")).toBeInTheDocument();
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: "Review and save" }));
    const dialog = await screen.findByRole("dialog", { name: "Save a new version?" });
    expect(within(dialog).getByText("policy.auto_max_lines")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save version" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ base_version: "v3", spec_patch: { policy: { auto_max_lines: 5 } } });
    // the new version is loaded
    expect(await screen.findByText("v4")).toBeInTheDocument();
  });

  it("gateway unreachable (catalog gateway_available: false): saved aliases stay as they are, the choice is locked", async () => {
    // Engine `agent_specs.catalog` sends aliases: [] and gateway_available: false when LiteLLM is down.
    Object.assign(server.catalog, { aliases: [], gateway_available: false });
    const { container } = renderAdmin(<AgentsPage />);
    const card = await screen.findByRole("region", { name: "Recommender" });
    const select = within(card).getByLabelText(/Model alias/);
    expect(select).toBeDisabled();
    expect(select).toHaveDisplayValue("reasoning-default");
    expect(within(card).queryByText(/not available/)).toBeNull();
    expect(select).toHaveAccessibleDescription(/AI Gateway can't be reached/);
    await settle();
    await expectNoA11yViolations(container);
  });

  it("platform admin may raise guarded fields (no lock)", async () => {
    const user = userEvent.setup();
    renderAdmin(<AgentsPage />, AVA());
    const lines = await screen.findByLabelText(/Auto-apply limit \(lines\)/);
    expect(lines).not.toHaveAccessibleDescription(/second admin/);
    await user.clear(lines);
    await user.type(lines, "25");
    expect(document.querySelector("[data-second-admin]")).toBeNull();
    expect(screen.getByRole("button", { name: "Review and save" })).not.toHaveAttribute("aria-disabled", "true");
  });

  it("tool allowlist: untrusted / write / unreviewed tools are disabled with a reason", async () => {
    renderAdmin(<AgentsPage />);
    const allowed = await screen.findAllByRole("group", { name: /Allowed tools/ });
    const box = (name: string) => within(allowed[0]!).getByRole("checkbox", { name });
    expect(box("get_inventory")).toBeChecked();
    expect(box("get_demand_forecast")).toBeEnabled();
    expect(box("list_supplier_messages")).toBeDisabled();
    expect(box("list_supplier_messages")).toHaveAccessibleDescription(/untrusted text/);
    expect(box("apply_change_set")).toBeDisabled();
    expect(box("apply_change_set")).toHaveAccessibleDescription(/Changes data/);
    expect(box("list_sailings")).toHaveAccessibleDescription(/Needs review/);
  });

  it("tenant admin: lowering 'Approvals needed' needs a second admin (M7 change request); raising is fine", async () => {
    const user = userEvent.setup();
    server.spec.spec.policy.approval_quorum = 2;
    renderAdmin(<AgentsPage />);
    const quorum = await screen.findByLabelText("Approvals needed");
    expect(quorum).toHaveAccessibleDescription(/Lowering it needs a second admin/);
    await user.clear(quorum);
    await user.type(quorum, "1");
    expect(await screen.findByText(/lowering it below 2/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review and request approval" })).not.toHaveAttribute("aria-disabled", "true");
    await user.clear(quorum);
    await user.type(quorum, "3");
    expect(document.querySelector("[data-second-admin]")).toBeNull();
    expect(screen.getByRole("button", { name: "Review and save" })).not.toHaveAttribute("aria-disabled", "true");
  });

  it("model aliases: a role can't pick a non-chat alias, tool-using roles need tool-loop support", async () => {
    renderAdmin(<AgentsPage />);
    const option = (role: string, alias: string) =>
      within(within(screen.getByRole("region", { name: role })).getByLabelText("Model alias")).getByRole("option", { name: new RegExp(`^${alias}`) });
    await screen.findByRole("region", { name: "Recommender" });
    expect(option("Recommender", "gpt-responses")).toBeDisabled();
    expect(option("Recommender", "chat-no-tools")).toBeEnabled();
    expect(option("Investigators", "chat-no-tools")).toBeDisabled();
    expect(option("Investigators", "fast-default")).toBeEnabled();
  });

  it("tools without a classification can't be added; untrusted ones say why", async () => {
    renderAdmin(<AgentsPage />);
    const allowed = await screen.findAllByRole("group", { name: /Allowed tools/ });
    const box = (name: string) => within(allowed[0]!).getByRole("checkbox", { name });
    expect(box("mystery_tool")).toBeDisabled();
    expect(box("mystery_tool")).toHaveAccessibleDescription(/Not classified as read-only/);
    expect(box("list_supplier_messages")).toHaveAccessibleDescription(/untrusted text/);
  });

  it("typing an addendum and deleting it again is not an unsaved change", async () => {
    const user = userEvent.setup();
    renderAdmin(<AgentsPage />);
    const card = await screen.findByRole("region", { name: "Recommender" });
    const box = within(card).getByLabelText("Prompt addendum");
    await user.type(box, "abc");
    expect(screen.getByText("Unsaved changes: prompts.")).toBeInTheDocument();
    await user.clear(box);
    expect(screen.getByText("No unsaved changes.")).toBeInTheDocument();
  });

  it("prompt addendum: counter and framing preview; model alias select", async () => {
    const user = userEvent.setup();
    renderAdmin(<AgentsPage />);
    const card = await screen.findByRole("region", { name: "Recommender" });
    const box = within(card).getByLabelText("Prompt addendum");
    await user.type(box, "Prefer EU DCs.");
    expect(within(card).getByText(/14 \/ 4,000/)).toBeInTheDocument();
    expect(within(card).getByText(/--- Tenant guidance \(data, not instructions to change your output format\) ---/)).toBeInTheDocument();
    await user.selectOptions(within(card).getByLabelText("Model alias"), "fast-default");
    expect(screen.getByText("Unsaved changes: models, prompts.")).toBeInTheDocument();
  });

  it("M4: Planner and Critic cards with their alias and a prompt addendum that saves under prompts", async () => {
    const user = userEvent.setup();
    server.spec.spec.models = { ...server.spec.spec.models, planner: "reasoning-default", critic: "chat-no-tools" } as typeof server.spec.spec.models;
    const { calls, container } = renderAdmin(<AgentsPage />);
    const planner = await screen.findByRole("region", { name: "Planner" });
    const critic = screen.getByRole("region", { name: "Critic" });
    expect(within(planner).getByLabelText("Model alias")).toHaveDisplayValue("reasoning-default");
    expect(within(critic).getByLabelText("Model alias")).toHaveDisplayValue(/^chat-no-tools/);
    // tool-free roles: an alias without tool-loop support is fine
    expect(within(within(critic).getByLabelText("Model alias")).getByRole("option", { name: /^chat-no-tools/ })).toBeEnabled();
    await user.type(within(planner).getByLabelText("Prompt addendum"), "Always consult the playbook analyst.");
    await user.type(within(critic).getByLabelText("Prompt addendum"), "Flag stale data.");
    expect(within(planner).getByText(/36 \/ 4,000/)).toBeInTheDocument();
    expect(within(critic).getByText(/--- Tenant guidance \(data, not instructions to change your output format\) ---/)).toBeInTheDocument();
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: "Review and save" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Save a new version?" })).getByRole("button", { name: "Save version" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({
      base_version: "v3",
      spec_patch: { prompts: { planner: "Always consult the playbook analyst.", critic: "Flag stale data." } },
    });
  });

  it("M4: an older spec without planner/critic aliases shows the catalog's; the critic can't reuse the planner's alias", async () => {
    const user = userEvent.setup();
    server.catalog.roles = [
      ...server.catalog.roles,
      { role: "planner", display_name: "Planner", alias: "fast-default", prompt_role: "planner", uses_tools: false },
      { role: "critic", display_name: "Critic", alias: "chat-no-tools", prompt_role: "critic", uses_tools: false },
    ];
    renderAdmin(<AgentsPage />);
    const planner = await screen.findByRole("region", { name: "Planner" });
    const critic = screen.getByRole("region", { name: "Critic" });
    expect(within(planner).getByLabelText("Model alias")).toHaveDisplayValue(/^fast-default/);
    expect(screen.getByText("No unsaved changes.")).toBeInTheDocument();
    await user.selectOptions(within(critic).getByLabelText("Model alias"), "fast-default");
    expect(await within(critic).findByText(/different model than the Planner/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review and save" })).toHaveAttribute("aria-disabled", "true");
  });

  it("Discard resets the draft and the typed numbers", async () => {
    const user = userEvent.setup();
    renderAdmin(<AgentsPage />);
    const quorum = await screen.findByLabelText("Approvals needed");
    await user.clear(quorum);
    await user.type(quorum, "3");
    expect(screen.getByText("Unsaved changes: policy.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByText("No unsaved changes.")).toBeInTheDocument();
    expect(screen.getByLabelText("Approvals needed")).toHaveValue(1);
  });

  it("a stale base version (409) says so and offers to load the latest", async () => {
    const user = userEvent.setup();
    renderAdmin(<AgentsPage />);
    const quorum = await screen.findByLabelText("Approvals needed");
    await user.clear(quorum);
    await user.type(quorum, "2");
    server.spec = { ...server.spec, version: "v9" }; // someone else saved meanwhile
    await user.click(screen.getByRole("button", { name: "Review and save" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Save version" }));
    expect(await screen.findByText(/Someone saved a newer version/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load the latest version" }));
    expect(await screen.findByText("v9")).toBeInTheDocument();
  });

  it("versions drawer: JSON diff against the active version, then restore", async () => {
    const user = userEvent.setup();
    const { calls } = renderAdmin(<AgentsPage />);
    await user.click(await screen.findByRole("button", { name: "Versions" }));
    const drawer = await screen.findByRole("dialog", { name: "Versions" });
    await user.click(await within(drawer).findByRole("button", { name: /^v2/ }));
    const diff = await within(drawer).findByRole("table", { name: "Changes from v2 to the active v3" });
    expect(within(diff).getByText("models.recommender")).toBeInTheDocument();
    await expectNoA11yViolations(drawer);
    await user.click(within(drawer).getByRole("button", { name: "Restore v2" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Restore v2?" });
    await user.click(within(confirm).getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/agents/spec/restore")).toBe(true));
    expect(calls.find((c) => c.url === "/api/admin/agents/spec/restore")!.body).toEqual({ version: "v2" });
  });
});

describe("Connectors", () => {
  it("lists connectors with tier badges and review counts", async () => {
    const { container } = renderAdmin(<ConnectorsPage />);
    const table = await screen.findByRole("table", { name: "Registered connectors" });
    expect(within(table).getByRole("link", { name: "Merxa Retail Suite" })).toHaveAttribute("href", "/admin/connectors/merxa");
    expect(within(table).getByText("Tier W: change sets (write)")).toBeInTheDocument();
    expect(within(table).getByText("1 to review")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add connector" })).toHaveAttribute("href", "/admin/connectors/new");
    await expectNoA11yViolations(container);
  });

  it("wizard: basics → auth (draft) → live test log → review tools → activate", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderAdmin(<ConnectorWizard />);
    // Basics: validation on submit
    await user.click(screen.getByRole("button", { name: "Next: authentication" }));
    expect(screen.getByText("Give the connector a name.")).toBeInTheDocument();
    await user.type(screen.getByLabelText(/^Name/), "ERP Lite");
    expect(screen.getByLabelText(/^Slug/)).toHaveValue("erp-lite");
    await user.type(screen.getByLabelText(/^MCP server URL/), "http://merxa.localhost/mcp-lite");
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: "Next: authentication" }));

    // Auth
    expect(await screen.findByRole("heading", { name: "2. Authentication" })).toHaveFocus();
    await user.type(screen.getByLabelText(/^Header name/), "X-Merxa-Api-Key");
    await user.type(screen.getByLabelText(/^Header value/), "s3cret");
    await user.click(screen.getByRole("button", { name: "Save draft and test" }));

    // Test: the draft is created then tested; the step log shows every step
    const log = await screen.findByRole("list", { name: "Connection test" });
    await waitFor(() => expect(within(log).getAllByText("Passed")).toHaveLength(7));
    const create = calls.find((c) => c.method === "POST" && c.url === "/api/admin/connectors")!;
    expect(create.body).toEqual({
      slug: "erp-lite",
      name: "ERP Lite",
      mcp_url: "http://merxa.localhost/mcp-lite",
      trust_level: "third_party",
      auth: { kind: "static_header", header_name: "X-Merxa-Api-Key", secret: "s3cret" },
    });
    expect(calls.some((c) => c.url === "/api/admin/connectors/erp-lite/test")).toBe(true);
    expect(screen.getByText(/Test passed: 2 tools found/)).toBeInTheDocument();
    expect(within(log).getByText("fallback: initialize")).toBeInTheDocument();
    await expectNoA11yViolations(container);

    // Review: needs-review rows highlighted; write → read locked for a tenant admin
    await user.click(screen.getByRole("button", { name: "Next: review tools" }));
    const table = await screen.findByRole("table", { name: "Discovered tools" });
    expect(screen.getByText("2 need review")).toBeInTheDocument();
    expect(table.querySelectorAll("tr[data-status=needs_review]")).toHaveLength(2);
    const access = within(table).getByRole("combobox", { name: "Access of update_item" });
    // M7: a tenant admin may ask for it; the server answers with a change request.
    expect(within(access).getByRole("option", { name: "Read (needs a second admin)" })).toBeEnabled();
    expect(within(table).getAllByText("Read needs a second admin").length).toBeGreaterThan(0);
    await user.click(within(table).getByRole("button", { name: "Approve list_items" }));
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    expect(calls.find((c) => c.method === "PATCH")).toMatchObject({ url: "/api/admin/connectors/erp-lite/tools/list_items", body: { status: "approved" } });
    await expectNoA11yViolations(container);

    // Activate
    await user.click(screen.getByRole("button", { name: "Next: activate" }));
    expect(screen.getByText(/1 tool still needs review/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activate connector" }));
    expect(await screen.findByText("ERP Lite is active.")).toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/admin/connectors/erp-lite/activate")).toBe(true);
  });

  it("wizard: first party needs a platform admin (the engine answers 403 for a tenant admin)", async () => {
    const { unmount } = renderAdmin(<ConnectorWizard />);
    const trust = screen.getByLabelText(/^Trust level/);
    expect(within(trust).getByRole("option", { name: /First party/ })).toBeDisabled();
    expect(trust).toHaveAccessibleDescription(/First party requires platform admin/);
    unmount();
    renderAdmin(<ConnectorWizard />, AVA());
    expect(within(screen.getByLabelText(/^Trust level/)).getByRole("option", { name: /First party/ })).toBeEnabled();
  });

  it("tools table (M7): approving a tool suggested as write but stored as read, or marking untrusted text trusted, needs a second admin", async () => {
    server.tools = [
      { name: "sneaky_write", access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none", status: "needs_review", suggested: { access: "write", content_trust: "trusted_data" } },
      { name: "list_notes", access: "read", content_trust: "untrusted_text", egress: "internal", idempotency: "none", status: "needs_review", suggested: { access: "read", content_trust: "untrusted_text" } },
    ];
    const { container, unmount } = renderAdmin(<ConnectorDetail slug="merxa" />);
    const table = await screen.findByRole("table", { name: "Tools" });
    const approve = within(table).getByRole("button", { name: "Approve sneaky_write" });
    expect(approve).not.toHaveAttribute("aria-disabled", "true");
    expect(approve).toHaveAccessibleDescription(/approving it as read needs a second admin/);
    expect(within(table).getByRole("button", { name: "Approve list_notes" })).not.toHaveAttribute("aria-disabled", "true");
    const trust = within(table).getByRole("combobox", { name: "Content of list_notes" });
    expect(within(trust).getByRole("option", { name: "Trusted data (needs a second admin)" })).toBeEnabled();
    expect(within(table).getByText("Trusted data needs a second admin")).toBeInTheDocument();
    await expectNoA11yViolations(container);
    unmount();
    renderAdmin(<ConnectorDetail slug="merxa" />, AVA());
    const t2 = await screen.findByRole("table", { name: "Tools" });
    expect(within(t2).getByRole("button", { name: "Approve sneaky_write" })).not.toHaveAttribute("aria-disabled", "true");
    expect(within(within(t2).getByRole("combobox", { name: "Content of list_notes" })).getByRole("option", { name: "Trusted data" })).toBeEnabled();
  });

  it("detail: shows the stored last test and trust level from GET /connectors/{slug}", async () => {
    const { calls } = renderAdmin(<ConnectorDetail slug="merxa" />);
    const log = await screen.findByRole("list", { name: "Connection test" });
    expect(within(log).getByText("merxa.localhost (dev host)")).toBeInTheDocument();
    expect(screen.getByText("First party (your own system)")).toBeInTheDocument();
    expect(screen.getByText("/api/webhooks/demo-retail/merxa")).toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/admin/connectors/merxa")).toBe(true);
  });

  it("wizard: a failed test keeps Review locked and can be re-run", async () => {
    const user = userEvent.setup();
    server.test = {
      ok: false,
      tier: [],
      steps: [
        { step: "url_policy", ok: true, ms: 1 },
        { step: "reach", ok: false, ms: 3001, detail: "connect timeout" },
      ],
      tools: [],
    };
    renderAdmin(<ConnectorWizard />);
    await user.type(screen.getByLabelText(/^Name/), "Broken");
    await user.type(screen.getByLabelText(/^MCP server URL/), "https://erp.example.com/mcp");
    await user.click(screen.getByRole("button", { name: "Next: authentication" }));
    await user.selectOptions(screen.getByLabelText("Method"), "none");
    await user.click(screen.getByRole("button", { name: "Save draft and test" }));
    const log = await screen.findByRole("list", { name: "Connection test" });
    await waitFor(() => expect(within(log).getByText("Failed")).toBeInTheDocument());
    expect(within(log).getAllByText("Skipped")).toHaveLength(5);
    expect(screen.getByText(/The test failed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next: review tools" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: /Review tools/ })).toBeDisabled(); // stepper
    server.test = { ...server.test, ok: true, steps: [{ step: "url_policy", ok: true, ms: 1 }, { step: "reach", ok: true, ms: 9 }] };
    await user.click(screen.getByRole("button", { name: "Run test again" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Next: review tools" })).not.toHaveAttribute("aria-disabled", "true"));
  });

  it("platform admin may reclassify a write tool as read", async () => {
    renderAdmin(<ConnectorDetail slug="merxa" />, AVA());
    const table = await screen.findByRole("table", { name: "Tools" });
    const access = within(table).getByRole("combobox", { name: "Access of update_item" });
    expect(within(access).getByRole("option", { name: "Read" })).toBeEnabled();
  });

  it("detail: tools, webhook events, test log and delete confirmation", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderAdmin(<ConnectorDetail slug="merxa" />);
    expect(await screen.findByRole("heading", { level: 1, name: "Merxa Retail Suite" })).toBeInTheDocument();
    const events = await screen.findByRole("table", { name: "Recent webhook events" });
    expect(within(events).getByText("po.updated")).toBeInTheDocument();
    expect(within(events).getByText("Not yet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run test" }));
    const log = await screen.findByRole("list", { name: "Connection test" });
    await waitFor(() => expect(within(log).getAllByText("Passed")).toHaveLength(7));
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Delete Merxa Retail Suite?" });
    // M7: deleting a connector is guarded for a tenant admin (the fake applies it right away).
    await user.click(within(confirm).getByRole("button", { name: "Request deletion" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/admin/connectors"));
    expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/admin/connectors/merxa")).toBe(true);
  });

  it("detail: unknown slug → not found", async () => {
    renderAdmin(<ConnectorDetail slug="nope" />);
    expect(await screen.findByRole("heading", { level: 1, name: "Connector not found" })).toBeInTheDocument();
  });

  it("detail: a tool the server no longer offers (status removed) is read-only, with no actions that would 409", async () => {
    // The engine's GET …/tools lists removed tools too; PATCHing one answers 409 tool_removed.
    server.tools = [
      ...server.tools,
      { name: "old_report", title: "Old report", access: "write", content_trust: "trusted_data", egress: "internal", idempotency: "none", status: "removed", suggested: { access: "write" } },
    ];
    const { calls, container } = renderAdmin(<ConnectorDetail slug="merxa" />);
    const row = (await screen.findByRole("rowheader", { name: /old_report/ })).closest("tr")!;
    expect(row).toHaveAttribute("data-status", "removed");
    expect(within(row).getByText("Removed")).toBeInTheDocument();
    expect(within(row).getByText("No longer offered by the server.")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Approve|Disable/ })).toBeNull();
    for (const select of within(row).getAllByRole("combobox")) expect(select).toBeDisabled();
    expect(within(row).queryByText(/needs platform admin/)).toBeNull();
    await settle();
    await expectNoA11yViolations(container);
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
  });
});

describe("Tenants (platform-admin)", () => {
  it("lists tenants and packs; creates a tenant from a pack", async () => {
    const user = userEvent.setup();
    const { calls, container } = renderAdmin(<TenantsPage />, AVA());
    expect(await screen.findByRole("rowheader", { name: "Isolation test tenant" })).toBeInTheDocument();
    await settle();
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: "Create tenant from pack" }));
    const dialog = await screen.findByRole("dialog", { name: "Create tenant from pack" });
    await user.click(within(dialog).getByRole("button", { name: "Create tenant" }));
    expect(within(dialog).getByText("Enter the workspace name.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Name/), "Acme Fashion");
    expect(within(dialog).getByLabelText(/^Slug/)).toHaveValue("acme-fashion");
    await user.selectOptions(within(dialog).getByLabelText(/^Template pack/), "blank");
    await user.type(within(dialog).getByLabelText(/^Admin usernames/), "sam, nora");
    await user.click(within(dialog).getByRole("button", { name: "Create tenant" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/admin/tenants")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ slug: "acme-fashion", name: "Acme Fashion", pack: "blank", admin_usernames: ["sam", "nora"] });
    expect(await screen.findByRole("rowheader", { name: "Acme Fashion" })).toBeInTheDocument();
  });

  it("says which admins did not get access (unknown username) and when the tenant already existed", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    const error = vi.spyOn(toast, "error");
    try {
      renderAdmin(<TenantsPage />, AVA());
      await screen.findByRole("rowheader", { name: "Isolation test tenant" });
      const create = async (slugName: string, pack: string, admins: string) => {
        await user.click(screen.getByRole("button", { name: "Create tenant from pack" }));
        const dialog = await screen.findByRole("dialog", { name: "Create tenant from pack" });
        await user.type(within(dialog).getByLabelText(/^Name/), slugName);
        await user.selectOptions(within(dialog).getByLabelText(/^Template pack/), pack);
        await user.type(within(dialog).getByLabelText(/^Admin usernames/), admins);
        await user.click(within(dialog).getByRole("button", { name: "Create tenant" }));
        await waitFor(() => expect(screen.queryByRole("dialog", { name: "Create tenant from pack" })).toBeNull());
      };
      await create("Acme Fashion", "blank", "sam, ghost");
      expect(success).toHaveBeenLastCalledWith("Created Acme Fashion", expect.anything());
      expect(error).toHaveBeenLastCalledWith("1 admin was not given access", { description: "ghost (no such user)" });
      error.mockClear();
      // Same slug + pack again: the engine re-provisions (200, created: false) — not "Created".
      await create("Acme Fashion", "blank", "sam");
      expect(success).toHaveBeenLastCalledWith("Acme Fashion already existed", expect.anything());
      expect(error).not.toHaveBeenCalled();
    } finally {
      success.mockRestore();
      error.mockRestore();
    }
  });

  it("parseUsernames dedupes and flags invalid names", () => {
    expect(parseUsernames("sam, nora sam;bad name!")).toEqual({ names: ["sam", "nora", "bad", "name!"], invalid: ["name!"] });
  });
});

describe("Audit", () => {
  it("filters by action and who; shows the change diff", async () => {
    const user = userEvent.setup();
    const { container } = renderAdmin(<AuditPage />);
    const table = await screen.findByRole("table", { name: "Admin audit log" });
    const rows = () => (table as HTMLTableElement).tBodies[0]!.rows;
    expect(rows()).toHaveLength(2);
    await user.selectOptions(screen.getByLabelText("Action"), "console.handoff");
    expect(rows()).toHaveLength(1);
    expect(screen.getByText("1 of 2 entries")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Action"), "");
    await user.type(screen.getByLabelText("Who"), "sam");
    expect(within(table).getByRole("rowheader", { name: "agent_spec.update" })).toBeInTheDocument();
    // who: the engine's actor_username, not "system"
    expect(within(table).getByRole("cell", { name: "sam" })).toBeInTheDocument();
    expect(within(table).queryByText("system")).toBeNull();
    await expectNoA11yViolations(container);
  });

  it("filterAudit matches text across action, target and tenant", () => {
    const rows = auditListSchema.parse(makeAdminServer().audit);
    expect(filterAudit(rows, { action: "", actor: "", text: "gateway" }).map((r) => r.action)).toEqual(["gateway.model.upsert", "console.handoff"]);
    expect(filterAudit(rows, { action: "", actor: "AVA", text: "" })).toHaveLength(2);
  });

  it("loads (it used to ask for 500 rows: 422) and pages with 'Load older entries'", async () => {
    const user = userEvent.setup();
    server.audit = Array.from({ length: 205 }, (_, i) => ({
      id: 205 - i,
      at: "2026-10-06T08:00:00Z",
      actor_username: "sam",
      tenant: "demo-retail",
      action: "connector.test",
      target: `connector:c${205 - i}`,
    }));
    const { calls } = renderAdmin(<AuditPage />);
    expect(await screen.findByText("200 entries")).toBeInTheDocument();
    expect(calls.find((c) => c.url.startsWith("/api/admin/audit?"))!.url).toBe("/api/admin/audit?limit=200");
    await user.click(screen.getByRole("button", { name: "Load older entries" }));
    expect(await screen.findByText("205 entries")).toBeInTheDocument();
    expect(calls.at(-1)!.url).toBe("/api/admin/audit?limit=200&before=6");
    expect(screen.queryByRole("button", { name: "Load older entries" })).toBeNull();
  });

  it("platform admin: scope switch to platform-wide rows; tenant admins have no scope", async () => {
    const user = userEvent.setup();
    const { unmount } = renderAdmin(<AuditPage />);
    await screen.findByRole("table", { name: "Admin audit log" });
    expect(screen.queryByLabelText("Scope")).toBeNull();
    unmount();
    const { calls, container } = renderAdmin(<AuditPage />, AVA());
    const table = await screen.findByRole("table", { name: "Admin audit log" });
    expect(within(table).queryByRole("rowheader", { name: "gateway.model.upsert" })).toBeNull();
    await user.selectOptions(screen.getByLabelText("Scope"), "platform");
    expect(await screen.findByRole("rowheader", { name: "gateway.model.upsert" })).toBeInTheDocument();
    expect(calls.at(-1)!.url).toBe("/api/admin/audit?limit=200&scope=platform");
    expect(calls.every((c) => !c.url.includes("tenant="))).toBe(true);
    await expectNoA11yViolations(container);
  });
});
