/**
 * M8 signal clustering in the web (`docs/contracts/123`): tolerant membership parsing, the alert
 * drawer's "Why these reports are grouped" section (method chips, evidence, the server sentence,
 * near-duplicates under their original, headlines as plain text, hidden without memberships, axe)
 * and the Signals admin "Clustering" section (read-only for a tenant admin, validated edits and the
 * `ClusteringConfigUpdate` body for a platform admin, the 409 reload message, hidden on an older engine).
 */
import { signalsAlertDetailSchema, signalsClusteringConfigUpdateSchema, signalsClusteringStatusSchema } from "@keelson/contracts";
import { Toaster } from "@keelson/ui";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SignalsClusteringSection } from "@/components/admin/SignalsClusteringSection";
import { AlertDrawer } from "@/components/alerts/AlertDrawer";
import { clusteringProblems, clusteringViewSchema, draftOf, pctTextToBp, remoteText, updateBody } from "@/lib/admin/signalsClustering";
import { AlertChannelProvider } from "@/lib/alerts/channel";
import { groupingRows, membershipChips, similarityPct } from "@/lib/alerts/grouping";
import { alertDetailResponseSchema } from "@/lib/alerts/types";
import { expectNoA11yViolations } from "./a11y";
import { FakeEventSource } from "./fakeEventSource";
import {
  COPY_ID,
  HTML_TITLE,
  MEMBERSHIPS,
  ORIGINAL_ID,
  STRIKE_ALERT_ID,
  TERMINAL_ID,
  clusteringConfig,
  clusteringStatus,
  strikeDetail,
} from "./fixtures/m8-signals";
import { fakeApi, jsonResponse, makeMe, withSession, type Route } from "./session";

const nav = vi.hoisted(() => ({ pathname: "/alerts", push: vi.fn(), refresh: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ push: nav.push, refresh: nav.refresh }) }));
vi.setConfig({ testTimeout: 30_000 });

beforeEach(() => {
  FakeEventSource.reset();
});

// ───────────────────────────── parsing + rows ─────────────────────────────

describe("memberships (lib)", () => {
  it("fixtures follow the contract", () => {
    expect(signalsAlertDetailSchema.safeParse(strikeDetail()).success).toBe(true);
    expect(signalsClusteringStatusSchema.safeParse(clusteringStatus(true)).success).toBe(false); // `editable` is the engine's extra
    const { editable: _e, ...status } = clusteringStatus(true);
    expect(signalsClusteringStatusSchema.safeParse(status).success).toBe(true);
  });

  it("keeps valid memberships of the event's own signals; skips malformed and foreign ones", () => {
    const bad = [
      ...MEMBERSHIPS,
      { ...MEMBERSHIPS[2], method: "guessed" },
      { ...MEMBERSHIPS[2], signal_id: "99999999-9999-5999-8999-999999999999" },
      { ...MEMBERSHIPS[2], event_id: "99999999-9999-5999-8999-999999999998" },
      "nope",
    ];
    const parsed = alertDetailResponseSchema.parse(strikeDetail({ memberships: bad }));
    expect(parsed.memberships.map((m) => m.signal_id)).toEqual([TERMINAL_ID, COPY_ID, ORIGINAL_ID]);
    expect(alertDetailResponseSchema.parse(strikeDetail({ memberships: null })).memberships).toEqual([]); // an older hub
  });

  it("rows: the first report first, the copy under its original, evidence chips", () => {
    const rows = groupingRows(alertDetailResponseSchema.parse(strikeDetail()));
    expect(rows.map((r) => [r.signalId, r.label])).toEqual([
      [ORIGINAL_ID, "First report"],
      [TERMINAL_ID, "Similar description"],
    ]);
    expect(rows[0]!.copies).toEqual([
      { signalId: COPY_ID, source: "Simulated Shipping Wire (demo replay) (Headline and link only)", headline: "Dockworkers strike stops container handling at the Port of Hamburg", bits: 3 },
    ]);
    expect(rows[1]!.chips).toEqual(["similarity 80 %"]);
    expect(membershipChips({ ...MEMBERSHIPS[2]!, method: "attach_ref", distance_km: 1250, shared_refs: ["UNLOCODE:A1AAA", "ISO3166:AA", "REGION:X", "LANE:L1"] })).toEqual([
      "1,250 km",
      "UNLOCODE:A1AAA",
      "ISO3166:AA",
      "REGION:X",
      "+1 more",
    ]);
    expect(similarityPct(7949)).toBe("79 %");
    expect(similarityPct(7950)).toBe("80 %");
    // an orphan copy (original not shown) is its own row; superseded versions are hidden
    const orphan = groupingRows({ ...alertDetailResponseSchema.parse(strikeDetail({ memberships: [MEMBERSHIPS[1]] })) });
    expect(orphan.map((r) => r.label)).toEqual(["Near-duplicate"]);
    const d = alertDetailResponseSchema.parse(strikeDetail());
    const versioned = { ...d, signals: d.signals.map((s) => (s.id === TERMINAL_ID ? { ...s, supersedes: ORIGINAL_ID } : s)) };
    expect(groupingRows(versioned).map((r) => r.signalId)).toEqual([COPY_ID, TERMINAL_ID]); // the copy's original is hidden
  });
});

// ───────────────────────────── drawer ─────────────────────────────

function renderDrawer(detail: unknown) {
  const { api } = fakeApi([(url) => (url === `/api/alerts/${STRIKE_ALERT_ID}` ? jsonResponse(200, detail) : undefined)]);
  const Wrapper = withSession(makeMe(), api);
  return render(
    <Wrapper>
      <Toaster />
      <AlertChannelProvider createEventSource={FakeEventSource.factory}>
        <AlertDrawer alertId={STRIKE_ALERT_ID} onClose={() => {}} canWrite={false} />
      </AlertChannelProvider>
    </Wrapper>,
  );
}

describe("alert drawer: why these reports are grouped", () => {
  it("shows the method chips, evidence and the server sentence; headlines stay text; axe", async () => {
    renderDrawer(strikeDetail());
    const drawer = await screen.findByRole("dialog", { name: "Dockworkers strike halts container handling at the Port of Hamburg" });
    const section = await within(drawer).findByRole("region", { name: "Why these reports are grouped" });
    const items = within(section).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("First report");
    expect(items[0]).toHaveTextContent("First report of this event: no open event matched it when it arrived.");
    expect(within(items[0]!).getByText(/Also reported by Simulated Shipping Wire \(demo replay\).*3 of 64 bits differ.*counted once/)).toBeInTheDocument();
    expect(within(items[1]!).getByText("Similar description")).toBeInTheDocument();
    expect(within(items[1]!).getByText("similarity 80 %")).toBeInTheDocument();
    expect(within(items[1]!).getByText(/Describes a similar disruption \(similarity 80 %, local term profile\)/)).toBeInTheDocument();
    // the untrusted headline is plain text, labelled as reported text: no markup is ever rendered
    expect(items[1]).toHaveTextContent(`Reported text: ${HTML_TITLE}`);
    expect(section.querySelector("b, img")).toBeNull();
    // the section sits between Sources and Updates
    const regions = within(drawer).getAllByRole("region").map((r) => r.getAttribute("aria-label"));
    expect(regions.indexOf("Why these reports are grouped")).toBe(regions.indexOf("Sources") + 1);
    expect(regions.indexOf("Updates")).toBe(regions.indexOf("Why these reports are grouped") + 1);
    await expectNoA11yViolations(drawer);
  });

  it("is hidden when the hub sends no memberships (an older hub) or only malformed ones", async () => {
    const { unmount } = renderDrawer(strikeDetail({ memberships: null }));
    const drawer = await screen.findByRole("dialog");
    await within(drawer).findByRole("region", { name: "Updates" });
    expect(within(drawer).queryByRole("region", { name: "Why these reports are grouped" })).toBeNull();
    unmount();
    renderDrawer(strikeDetail({ memberships: [{ ...MEMBERSHIPS[0], method: "guessed" }] }));
    const again = await screen.findByRole("dialog");
    await within(again).findByRole("region", { name: "Updates" });
    expect(within(again).queryByRole("region", { name: "Why these reports are grouped" })).toBeNull();
  });
});

// ───────────────────────────── admin section ─────────────────────────────

describe("clustering settings (lib)", () => {
  it("validation mirrors the contract bounds", () => {
    const d = draftOf(clusteringConfig());
    expect(clusteringProblems(d)).toEqual({});
    const bad = draftOf(clusteringConfig());
    bad.simhash.max_hamming_bits = 8;
    bad.simhash.window_hours = 0;
    bad.vector.max_distance_km = 10;
    bad.vector.local.merge_bp = 6000;
    bad.vector.remote.attach_bp = 4000;
    bad.vector.remote.strict_bp = 10_500;
    expect(clusteringProblems(bad)).toEqual({
      "simhash.max_hamming_bits": "0 to 7 bits.",
      "simhash.window_hours": "A whole number of hours, 1 to 168.",
      "vector.max_distance_km": "A whole number of km, 50 to 3000.",
      "vector.local.merge_bp": "At least the attach threshold.",
      "vector.remote.attach_bp": "Between 50 % and 99 %.",
      "vector.remote.strict_bp": "Between 0 % and 100 %.",
    });
    expect(signalsClusteringConfigUpdateSchema.safeParse(updateBody(3, d)).success).toBe(true);
    expect(pctTextToBp("72,5")).toBe(7250);
    expect(Number.isNaN(pctTextToBp(" "))).toBe(true);
    const view = clusteringViewSchema.parse(clusteringStatus(false));
    expect(view.editable).toBe(false);
    expect(remoteText(view)).toBe("Off (local term vectors only)");
    expect(remoteText(clusteringViewSchema.parse(clusteringStatus(true, {
      vector_backend: { pgvector: true, local_profile: "local-terms-v1", remote: { alias: "embed-default", dims: 1536, storage: "vector(1536)", status: "backfilling", pending: 1200 }, remote_reason: null },
    })))).toBe("embed-default@1536 (vector(1536)), embedding 1,200 reports");
  });
});

function renderSection(roles: string[], routes: Route[]) {
  const { api, calls } = fakeApi(routes);
  const Wrapper = withSession(makeMe({ roles }), api);
  const utils = render(
    <Wrapper>
      <Toaster />
      <SignalsClusteringSection />
    </Wrapper>,
  );
  return { ...utils, calls };
}

const statusRoute = (editable: boolean): Route => (url, init) =>
  url === "/api/admin/signals/clustering" && (init.method ?? "GET") === "GET" ? jsonResponse(200, clusteringStatus(editable)) : undefined;

describe("Signals admin: Clustering", () => {
  it("a tenant admin sees the values read-only with the platform note; axe", async () => {
    const { container } = renderSection(["tenant-admin"], [statusRoute(false)]);
    const form = await screen.findByRole("form", { name: "Clustering settings" });
    expect(screen.getByText(/Requires platform admin: clustering applies to every workspace/)).toBeInTheDocument();
    for (const input of within(form).getAllByRole("textbox")) expect(input).toBeDisabled();
    for (const sw of within(form).getAllByRole("switch")) expect(sw).toBeDisabled();
    expect(within(form).queryByRole("button", { name: "Save clustering settings" })).toBeNull();
    expect(within(form).getByLabelText("Local merge (%)")).toHaveValue("75");
    const table = screen.getByRole("table", { name: "Grouping in the last 7 days" });
    expect(within(table).getByRole("rowheader", { name: "Joined by similar description" }).closest("tr")).toHaveTextContent("1");
    expect(screen.getByText("Off (local term vectors only)")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("a platform admin edits: validation blocks merge < attach; the PUT is a ClusteringConfigUpdate", async () => {
    const user = userEvent.setup();
    const { calls } = renderSection(["platform-admin"], [
      statusRoute(true),
      (url, init) => (url === "/api/admin/signals/clustering" && init.method === "PUT" ? jsonResponse(200, clusteringConfig({ version: 4 })) : undefined),
      (url) => (url === "/api/admin/signals/clustering/run" ? jsonResponse(200, { merged: [{}], embedded: 0 }) : undefined),
    ]);
    const form = await screen.findByRole("form", { name: "Clustering settings" });
    const save = within(form).getByRole("button", { name: "Save clustering settings" });
    expect(save).toHaveAttribute("aria-disabled", "true"); // nothing changed yet
    const merge = within(form).getByLabelText("Local merge (%)");
    await user.clear(merge);
    await user.type(merge, "65");
    expect(within(form).getByText("At least the attach threshold.")).toBeInTheDocument();
    expect(save).toHaveAttribute("aria-disabled", "true");
    await user.clear(merge);
    await user.type(merge, "80");
    await user.selectOptions(within(form).getByLabelText("Most differing bits (of 64)"), "4");
    await user.click(within(form).getByRole("switch", { name: "Reuse the original's extraction" }));
    expect(save).not.toHaveAttribute("aria-disabled", "true");
    await user.click(save);
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const body = calls.find((c) => c.method === "PUT")!.body;
    expect(signalsClusteringConfigUpdateSchema.safeParse(body).success).toBe(true);
    expect(body).toEqual({
      base_version: 3,
      simhash: { enabled: true, max_hamming_bits: 4, window_hours: 72, reuse_extraction: false },
      vector: { ...clusteringConfig().vector, local: { attach_bp: 7000, merge_bp: 8000, strict_bp: 7500 } },
      updated_by: null,
    });
    await user.click(within(form).getByRole("button", { name: "Run clustering now" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/signals/clustering/run" && c.method === "POST")).toBe(true));
  });

  it("a stale base shows the reload message", async () => {
    const user = userEvent.setup();
    renderSection(["platform-admin"], [
      statusRoute(true),
      (url, init) =>
        url === "/api/admin/signals/clustering" && init.method === "PUT"
          ? jsonResponse(409, { code: "stale_version", message: "the clustering settings were changed by someone else", current_version: 4 })
          : undefined,
    ]);
    const form = await screen.findByRole("form", { name: "Clustering settings" });
    await user.click(within(form).getByRole("switch", { name: "Detect syndicated copies" }));
    await user.click(within(form).getByRole("button", { name: "Save clustering settings" }));
    expect(await within(form).findByText(/Settings changed by someone else — reload them/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("is hidden when the engine has no clustering settings (404)", async () => {
    const { container, calls } = renderSection(["platform-admin"], []);
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/signals/clustering")).toBe(true));
    await waitFor(() => expect(container.querySelector("[aria-busy='true']")).toBeNull());
    expect(screen.queryByRole("heading", { name: "Clustering" })).toBeNull();
  });
});
