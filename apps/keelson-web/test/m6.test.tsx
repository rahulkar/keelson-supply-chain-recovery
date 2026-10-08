import { blockEnvelopeSchema, networkMapDataSchema, signalsAlertDetailSchema, signalsAlertSchema, type BlockEnvelope } from "@keelson/contracts";
import { Toaster } from "@keelson/ui";
import { getFixture } from "@keelson/viz/fixtures";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AlertsInbox, applyUpdate, matchesFilters, sortAlerts } from "@/components/alerts/AlertsInbox";
import { TurnView } from "@/components/case/TurnView";
import { NewAlertsPill, newAlertsLabel } from "@/components/home/NewAlertsPill";
import { PersonaSwitcher } from "@/components/shell/PersonaSwitcher";
import { packProgress } from "@/components/shell/DemoPacks";
import { AlertChannelProvider, parseAlertEvent, shouldToast, TOAST_MAX_AGE_MS, type AlertUpdate } from "@/lib/alerts/channel";
import { ALERT_LIST_MAX, AlertsApi, alertPath } from "@/lib/alerts/client";
import {
  alertMapBlock,
  alertSources,
  alertTimeline,
  attribution,
  exposureBreakdown,
  exposureMethodText,
  exposureSummaryText,
  footprintPolygons,
  relativeTime,
  safeSourceUrl,
} from "@/lib/alerts/format";
import { alertDetailResponseSchema, alertListSchema, demoPackResultSchema, demoPacksSchema, type Alert } from "@/lib/alerts/types";
import { initialCaseStreamState, type BlockEntry, type CaseStreamState, type RunEntry } from "@/lib/stream/reducer";
import { buildTurns, canvasBlocks } from "@/lib/thread";
import { expectNoA11yViolations } from "./a11y";
import { FakeEventSource } from "./fakeEventSource";
import { NEWS_ALERT_ID, QUAKE_ALERT_ID, TYPHOON_ALERT_ID, alert, newsAlert, quakeAlert, typhoonDetail } from "./fixtures/m6";
import { fakeApi, jsonResponse, makeMe, withSession, type Route } from "./session";

const nav = vi.hoisted(() => ({ pathname: "/alerts", push: vi.fn(), refresh: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.pathname, useRouter: () => ({ push: nav.push, refresh: nav.refresh }) }));
vi.setConfig({ testTimeout: 30_000 });

beforeEach(() => {
  FakeEventSource.reset();
  nav.push.mockReset();
  window.sessionStorage.clear();
  window.history.replaceState({}, "", "/alerts");
});

const ALERTS = [alert(), quakeAlert, newsAlert];

function customEvent(name: string, value: unknown): string {
  return JSON.stringify({ type: "CUSTOM", name, value, timestamp: Date.now() });
}

// ───────────────────────────── fixtures & schemas ─────────────────────────────

describe("M6 fixtures follow the contract", () => {
  it("alerts and the detail validate against the generated schemas", () => {
    for (const a of ALERTS) {
      const { kind: _k, ...plain } = a as Alert;
      expect(signalsAlertSchema.safeParse(plain).success).toBe(true);
    }
    expect(signalsAlertDetailSchema.safeParse(typhoonDetail()).success).toBe(true);
  });

  it("a list keeps valid alerts (with their kind), drops malformed ones and extra fields", () => {
    const parsed = alertListSchema.parse({ items: [alert({ extra_field: 1 } as never), { id: "nope" }, quakeAlert], new_count: 4 });
    expect(parsed.items.map((a) => a.id)).toEqual([TYPHOON_ALERT_ID, QUAKE_ALERT_ID]);
    expect(parsed.items[0]).not.toHaveProperty("extra_field");
    expect(parsed.items[0]!.kind).toBe("natural_hazard");
    expect(parsed.newCount).toBe(4);
    expect(parsed.skipped).toBe(1);
    expect(alertListSchema.parse([quakeAlert]).items).toHaveLength(1);
    // An unknown kind is dropped, never trusted as an icon key.
    expect(alertListSchema.parse([alert({ kind: "<svg>" } as never)]).items[0]!.kind).toBeUndefined();
  });

  it("a detail skips a malformed signal / exposure and keeps a valid engine map", () => {
    const d = typhoonDetail({ extra: { map: { nonsense: true } } });
    (d.signals as unknown[]).push({ id: "bad" });
    (d.exposures as unknown[]).push({ node_code: "" });
    const parsed = alertDetailResponseSchema.parse(d);
    expect(parsed.signals).toHaveLength(2);
    expect(parsed.exposures).toHaveLength(3);
    expect(parsed.map).toBeNull();
  });
});

// ───────────────────────────── formatting ─────────────────────────────

describe("alert formatting", () => {
  it("summarises exposure from the deterministic counts", () => {
    expect(exposureSummaryText(alert().exposure_summary)).toBe("3 sites · 4 lanes · 2 open PO lines");
    expect(exposureSummaryText(quakeAlert.exposure_summary)).toBe("1 site");
    expect(exposureSummaryText(newsAlert.exposure_summary)).toBe("1 lane");
    expect(
      exposureSummaryText({ nodes_by_type: { supplier: 0, factory: 0, port: 0, dc: 0, store: 0, lane: 0 }, open_po_lines: null, units_inbound: null }),
    ).toBe("No exposed sites");
    expect(exposureBreakdown(alert().exposure_summary)).toEqual(["1 supplier", "1 factory", "1 port", "4 lanes"]);
  });

  it("explains how a node was matched", () => {
    expect(exposureMethodText({ method: "intersects", distance_km: null })).toBe("Inside the affected area");
    expect(exposureMethodText({ method: "dwithin_km", distance_km: 1250 })).toBe("1,250 km from the affected area");
    expect(exposureMethodText({ method: "lane_crosses", distance_km: null })).toBe("Route crosses the affected area");
    expect(exposureMethodText({ method: "entity_match", distance_km: null })).toBe("Named in the report");
  });

  it("relative times", () => {
    const now = Date.parse("2026-10-08T06:00:00Z");
    expect(relativeTime("2026-10-08T05:59:40Z", now)).toBe("just now");
    expect(relativeTime("2026-10-08T05:48:00Z", now)).toBe("12 min ago");
    expect(relativeTime("2026-10-08T03:00:00Z", now)).toBe("3 h ago");
    expect(relativeTime("2026-10-07T05:00:00Z", now)).toBe("yesterday");
    expect(relativeTime("2026-09-01T05:00:00Z", now)).toBe("1 Sept 2026");
    expect(relativeTime("2026-10-08T07:00:00Z", now)).toBe("just now");
    expect(relativeTime("garbage", now)).toBe("garbage");
  });

  it("attributes sources with their licence and only links plain http(s) URLs", () => {
    expect(attribution({ name: "GDACS", license: "CC BY 4.0" })).toBe("GDACS (CC BY 4.0)");
    expect(attribution({ name: "USGS", license: "usgs" })).toBe("USGS");
    expect(safeSourceUrl("javascript:alert(1)")).toBeNull();
    expect(safeSourceUrl("https://user:pw@example.test/x")).toBeNull();
    expect(safeSourceUrl("https://www.gdacs.org/a?b=1")).toBe("https://www.gdacs.org/a?b=1");
    expect(safeSourceUrl(null)).toBeNull();
    const sources = alertSources(typhoonDetail().signals);
    expect(sources.map((s) => [s.attribution, s.provenance])).toEqual([
      ["GDACS (CC BY 4.0)", "simulated"],
      ["Demo Freight Daily (Headline and link only)", "inference"],
    ]);
  });

  it("lists updates newest first (signals with their version + the alert's milestones)", () => {
    const parsed = alertDetailResponseSchema.parse(typhoonDetail());
    const t = alertTimeline(parsed);
    expect(t.map((e) => e.text)).toEqual([
      "Tropical cyclone KAI-TAK, orange alert (update 4)",
      "Alert updated (new)",
      "Cat Lai terminal suspends operations ahead of the typhoon",
      "Alert raised",
    ]);
  });

  it("draws the event footprint as a valid network_map block (polygon, point, none, engine map)", () => {
    const parsed = alertDetailResponseSchema.parse(typhoonDetail());
    const block = alertMapBlock(parsed)!;
    expect(blockEnvelopeSchema.safeParse(block.envelope).success).toBe(true);
    expect(networkMapDataSchema.safeParse(block.data).success).toBe(true);
    expect(block.envelope.provenance?.kind).toBe("simulated");
    const fp = block.data.footprint!.features[0]!;
    expect(fp.geometry.type).toBe("Polygon");
    expect((fp.geometry.coordinates as number[][][])[0]![0]).toEqual([107_000_000, 9_500_000]);
    const [x0, y0, x1, y1] = (block.envelope.props as { bbox: number[] }).bbox;
    expect(x0).toBeLessThan(107_000_000);
    expect(x1).toBeGreaterThan(108_500_000);
    expect(y0).toBeLessThan(9_500_000);
    expect(y1).toBeGreaterThan(11_500_000);

    const point = alertDetailResponseSchema.parse(typhoonDetail());
    point.event = { ...point.event, geometry: { type: "Point", coordinates: [179.99, 89.9] } };
    const pb = alertMapBlock(point)!;
    expect(networkMapDataSchema.safeParse(pb.data).success).toBe(true);
    expect(blockEnvelopeSchema.safeParse(pb.envelope).success).toBe(true);
    expect(footprintPolygons({ type: "LineString", coordinates: [[0, 0], [1, 1]] })).toHaveLength(2);

    const none = alertDetailResponseSchema.parse(typhoonDetail());
    none.event = { ...none.event, geometry: null };
    expect(alertMapBlock(none)).toBeNull();

    const engineMap = alertDetailResponseSchema.parse(
      typhoonDetail({
        extra: {
          map: {
            props: { title: "x", summary: { nodes: 1, at_risk_nodes: 1, lanes: 0 }, bbox: [100_000_000, 5_000_000, 110_000_000, 15_000_000], legend: [] },
            data: { nodes: [{ id: "VNSGN", kind: "port", label: "Ho Chi Minh City", lat_e6: 10_760_000, lon_e6: 106_770_000, status: "disrupted", units_at_risk: null }], links: [], footprint: null },
          },
        },
      }),
    );
    const em = alertMapBlock(engineMap)!;
    expect(em.data.nodes).toHaveLength(1);
    expect(blockEnvelopeSchema.safeParse(em.envelope).success).toBe(true);
  });
});

// ───────────────────────────── client ─────────────────────────────

describe("AlertsApi", () => {
  it("never builds a path from a non-UUID id and refuses unknown actions / pack ids without calling", async () => {
    const { api, calls } = fakeApi([]);
    const alerts = new AlertsApi(api);
    expect(() => alertPath("../../api/demo/reset")).toThrow();
    await expect(alerts.get("../../demo/reset")).rejects.toMatchObject({ code: "invalid_request" });
    await expect(alerts.openCase("x?tenant=other", "k")).rejects.toMatchObject({ code: "invalid_request" });
    await expect(alerts.setStatus(TYPHOON_ALERT_ID, "resolved" as never)).rejects.toMatchObject({ code: "invalid_request" });
    await expect(alerts.packAction("../reset", "start")).rejects.toMatchObject({ code: "invalid_request" });
    await expect(alerts.packAction("typhoon-hcmc", "delete" as never)).rejects.toMatchObject({ code: "invalid_request" });
    expect(calls).toHaveLength(0);
  });

  it("calls the contract routes with CSRF and the open-case Idempotency-Key", async () => {
    const { api, calls } = fakeApi([
      (url) => (url.startsWith("/api/alerts?") ? jsonResponse(200, { items: ALERTS }) : undefined),
      (url) => (url.endsWith("/open-case") ? jsonResponse(200, { case_id: "case-1", run_id: "run-1" }) : undefined),
      (url) => (url.endsWith("/snooze") ? jsonResponse(200, { alert: alert({ status: "snoozed" }) }) : undefined),
      (url) => (url.endsWith("/dismiss") ? new Response(null, { status: 204 }) : undefined),
      (url) => (url === "/api/demo/packs/typhoon-hcmc/start" ? jsonResponse(200, { ok: true, step: 1, steps_total: 4 }) : undefined),
    ]);
    const alerts = new AlertsApi(api);
    expect((await alerts.list({ status: "new" })).items).toHaveLength(3);
    expect(calls[0]!.url).toBe("/api/alerts?status=new&limit=100");
    expect(await alerts.openCase(TYPHOON_ALERT_ID, "key-1")).toMatchObject({ case_id: "case-1" });
    const open = calls.at(-1)!;
    expect(open.url).toBe(`/api/alerts/${TYPHOON_ALERT_ID}/open-case`);
    expect(open.method).toBe("POST");
    expect(open.headers["Idempotency-Key"]).toBe("key-1");
    expect(open.headers["X-CSRF-Token"]).toBe("csrf-test");
    expect((await alerts.setStatus(TYPHOON_ALERT_ID, "snooze"))!.status).toBe("snoozed");
    expect(await alerts.setStatus(TYPHOON_ALERT_ID, "dismiss")).toBeNull();
    expect(await alerts.packAction("typhoon-hcmc", "start")).toMatchObject({ step: 1, steps_total: 4 });
  });
});

// ───────────────────────────── live channel ─────────────────────────────

describe("alert channel", () => {
  it("parses sc.alert.* CUSTOM events (alert as value or {alert, new_count}) and ignores the rest", () => {
    expect(parseAlertEvent(customEvent("sc.alert.raised", alert()))).toMatchObject({ name: "sc.alert.raised", alert: { id: TYPHOON_ALERT_ID } });
    expect(parseAlertEvent(customEvent("sc.alert.updated", { alert: quakeAlert, new_count: 2 }))).toMatchObject({ alert: { id: QUAKE_ALERT_ID }, newCount: 2 });
    expect(parseAlertEvent(customEvent("sc.case.updated", alert()))).toBeNull();
    expect(parseAlertEvent("not json")).toBeNull();
    expect(parseAlertEvent(customEvent("sc.alert.raised", { id: "bad" }))).toMatchObject({ alert: null });
  });

  it("toasts only fresh, new, high / critical alerts that were raised (once per severity)", () => {
    const now = Date.parse("2026-10-08T06:05:00Z");
    const raised = (a: Alert, name = "sc.alert.raised"): AlertUpdate => ({ name, alert: a, alertId: a.id, newCount: null });
    expect(shouldToast(raised(alert()), new Set(), now)).toBe(true);
    expect(shouldToast(raised(alert(), "sc.alert.updated"), new Set(), now)).toBe(false);
    expect(shouldToast(raised(alert({ severity: "medium" })), new Set(), now)).toBe(false);
    expect(shouldToast(raised(alert({ status: "seen" })), new Set(), now)).toBe(false);
    expect(shouldToast(raised(alert()), new Set([`${TYPHOON_ALERT_ID}:critical`]), now)).toBe(false);
    expect(shouldToast(raised(alert()), new Set([`${TYPHOON_ALERT_ID}:high`]), now)).toBe(true);
    // A stale real alert does not toast (a replayed backlog); DEMO alerts: see the review regressions.
    expect(shouldToast(raised(alert({ simulated: false })), new Set(), Date.parse("2026-10-08T06:00:00Z") + TOAST_MAX_AGE_MS + 1)).toBe(false);
  });

  function renderPill(routes: Route[] = []) {
    const { api, calls } = fakeApi([
      ...routes,
      (url) => (url.startsWith("/api/alerts?status=new") ? jsonResponse(200, { items: [alert(), alert({ id: NEWS_ALERT_ID })] }) : undefined),
    ]);
    const Wrapper = withSession(makeMe(), api);
    const onOpenAlert = vi.fn();
    const utils = render(
      <Wrapper>
        <Toaster />
        <AlertChannelProvider createEventSource={FakeEventSource.factory} onOpenAlert={onOpenAlert}>
          <NewAlertsPill />
        </AlertChannelProvider>
      </Wrapper>,
    );
    return { ...utils, calls, onOpenAlert };
  }

  it("Home pill: live count from the list, then from the stream; toast with 'View alert' on a new critical alert", async () => {
    const user = userEvent.setup();
    const { container, onOpenAlert } = renderPill();
    expect(screen.getByRole("link", { name: /Alerts|new alert/ })).toHaveAttribute("href", "/alerts");
    await screen.findByText("2 new alerts");
    const es = FakeEventSource.latest();
    expect(es.url).toBe("/api/alerts/events");
    const fresh = new Date().toISOString();
    act(() => {
      es.open();
      es.emit(customEvent("sc.alert.raised", alert({ id: QUAKE_ALERT_ID, severity: "critical", title: "Port <b>closed</b>", updated_at: fresh })));
    });
    await screen.findByText("3 new alerts");
    const toastTitles = await screen.findAllByText("Critical alert: Port <b>closed</b>");
    const shown = toastTitles.length;
    expect(container.ownerDocument.querySelector("b")).toBeNull();
    // The same alert again (e.g. a replayed event) does not toast twice.
    act(() => es.emit(customEvent("sc.alert.raised", alert({ id: QUAKE_ALERT_ID, severity: "critical", title: "Port <b>closed</b>", updated_at: fresh }))));
    expect(screen.getAllByText("Critical alert: Port <b>closed</b>")).toHaveLength(shown);
    expect(screen.getAllByRole("button", { name: "View alert", hidden: true })).toHaveLength(1);
    // Base UI keeps toast controls aria-hidden until the toast region is focused.
    await user.click(screen.getByRole("button", { name: "View alert", hidden: true }));
    expect(onOpenAlert).toHaveBeenCalledWith(QUAKE_ALERT_ID);
    // Seen elsewhere → no longer new.
    act(() => es.emit(customEvent("sc.alert.updated", alert({ status: "seen" }))));
    await screen.findByText("2 new alerts");
    await expectNoA11yViolations(container);
  });

  it("an older / medium alert updates the count without a toast", async () => {
    renderPill();
    await screen.findByText("2 new alerts");
    act(() => FakeEventSource.latest().emit(customEvent("sc.alert.raised", alert({ id: QUAKE_ALERT_ID, severity: "medium", updated_at: new Date().toISOString() }))));
    await screen.findByText("3 new alerts");
    expect(screen.queryByText(/alert:/)).toBeNull();
  });

  it("the pill says 'Alerts' when the count is unknown", async () => {
    expect(newAlertsLabel(null)).toBe("Alerts");
    expect(newAlertsLabel(0)).toBe("No new alerts");
    expect(newAlertsLabel(1)).toBe("1 new alert");
    const { api } = fakeApi([]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <AlertChannelProvider createEventSource={() => null}>
          <NewAlertsPill />
        </AlertChannelProvider>
      </Wrapper>,
    );
    await waitFor(() => expect(screen.getByRole("link")).toHaveTextContent("Alerts"));
  });

  it("reconnects after a fatal drop (session checked first) and re-reads the count", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { calls } = renderPill([(url) => (url === "/api/me" ? jsonResponse(200, makeMe()) : undefined)]);
      await screen.findByText("2 new alerts");
      act(() => FakeEventSource.latest().fail(true));
      await waitFor(() => expect(calls.some((c) => c.url === "/api/me")).toBe(true));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(FakeEventSource.instances).toHaveLength(2);
      await waitFor(() => expect(calls.filter((c) => c.url.startsWith("/api/alerts?status=new"))).toHaveLength(2));
    } finally {
      vi.useRealTimers();
    }
  });
});

// ───────────────────────────── inbox & drawer ─────────────────────────────

describe("Alerts inbox", () => {
  it("filters by status and severity; newest first", () => {
    const sorted = sortAlerts(ALERTS);
    expect(sorted.map((a) => a.id)).toEqual([TYPHOON_ALERT_ID, QUAKE_ALERT_ID, NEWS_ALERT_ID]);
    expect(ALERTS.filter((a) => matchesFilters(a, "open", "all")).map((a) => a.id)).toEqual([TYPHOON_ALERT_ID, QUAKE_ALERT_ID]);
    expect(ALERTS.filter((a) => matchesFilters(a, "all", "high")).map((a) => a.id)).toEqual([QUAKE_ALERT_ID]);
    expect(ALERTS.filter((a) => matchesFilters(a, "snoozed", "all")).map((a) => a.id)).toEqual([NEWS_ALERT_ID]);
  });

  function renderInbox(opts: { me?: ReturnType<typeof makeMe>; initial?: string | null; routes?: Route[] } = {}) {
    const { api, calls } = fakeApi([
      ...(opts.routes ?? []),
      (url) => (url.startsWith("/api/alerts?status=new") ? jsonResponse(200, { items: [alert()] }) : undefined),
      (url) => (url.startsWith("/api/alerts?status=snoozed") ? jsonResponse(200, { items: [newsAlert] }) : undefined),
      (url) => (url.startsWith("/api/alerts?") ? jsonResponse(200, { items: ALERTS }) : undefined),
      (url) => (url === `/api/alerts/${TYPHOON_ALERT_ID}` ? jsonResponse(200, typhoonDetail()) : undefined),
      (url) => (url.endsWith("/seen") ? jsonResponse(200, { alert: alert({ status: "seen" }) }) : undefined),
      (url) => (url.endsWith("/open-case") ? jsonResponse(200, { case_id: "case-from-alert", run_id: "run-signal-1" }) : undefined),
      (url) => (url.endsWith("/dismiss") ? jsonResponse(200, alert({ status: "dismissed" })) : undefined),
      (url) => (url.endsWith("/snooze") ? jsonResponse(200, alert({ status: "snoozed" })) : undefined),
    ]);
    const me = opts.me ?? makeMe();
    const Wrapper = withSession(me, api);
    const utils = render(
      <Wrapper>
        <Toaster />
        <AlertChannelProvider createEventSource={FakeEventSource.factory}>
          <AlertsInbox canWrite={me.user.roles.some((r) => ["planner", "approver", "tenant-admin"].includes(r))} initialAlertId={opts.initial ?? null} />
        </AlertChannelProvider>
      </Wrapper>,
    );
    return { ...utils, calls };
  }

  it("lists alerts calmly: severity, title, why, exposure, time, DEMO; no axe violations", async () => {
    const { container } = renderInbox();
    const list = await screen.findByRole("list", { name: "Alerts" });
    const rows = within(list).getAllByRole("button");
    expect(rows).toHaveLength(2); // "Needs attention" hides the snoozed one
    const first = rows[0]!;
    expect(first).toHaveAccessibleName("Tropical cyclone approaching Ho Chi Minh City");
    expect(within(first).getByText("Critical")).toBeInTheDocument();
    expect(within(first).getByText("3 sites · 4 lanes · 2 open PO lines")).toBeInTheDocument();
    expect(within(first).getByText("DEMO")).toBeInTheDocument();
    expect(within(first).getByText(/3 of your sites are inside the forecast cone/)).toBeInTheDocument();
    expect(within(rows[1]!).queryByText("DEMO")).toBeNull();
    expect(screen.getByText("2 alerts")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("filters: severity on the loaded list, a specific status from the server", async () => {
    const user = userEvent.setup();
    const { calls } = renderInbox();
    await screen.findByRole("list", { name: "Alerts" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Severity" }), "high");
    expect(within(screen.getByRole("list", { name: "Alerts" })).getAllByRole("button")).toHaveLength(1);
    await user.selectOptions(screen.getByRole("combobox", { name: "Severity" }), "low");
    expect(await screen.findByText("No alerts match these filters")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Severity" }), "all");
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "snoozed");
    await waitFor(() => expect(calls.some((c) => c.url === "/api/alerts?status=snoozed&limit=100")).toBe(true));
    const list = await screen.findByRole("list", { name: "Alerts" });
    // Untrusted headline markup stays text.
    expect(within(list).getByText("Strike announced at Felixstowe <b>terminal</b>")).toBeInTheDocument();
    expect(list.querySelector("b")).toBeNull();
  });

  it("the drawer shows exposure, map, sources with licence, updates; marks the alert seen; no axe violations", async () => {
    const user = userEvent.setup();
    const { calls } = renderInbox();
    await user.click(await screen.findByRole("button", { name: "Tropical cyclone approaching Ho Chi Minh City" }));
    const drawer = await screen.findByRole("dialog", { name: "Tropical cyclone approaching Ho Chi Minh City" });
    await within(drawer).findByText("GDACS (CC BY 4.0)");
    expect(window.location.search).toBe(`?alert=${TYPHOON_ALERT_ID}`);
    const gdacs = within(drawer).getByRole("link", { name: /GDACS \(CC BY 4\.0\)/ });
    expect(gdacs).toHaveAttribute("href", "https://www.gdacs.org/report.aspx?eventtype=TC&eventid=1000001");
    expect(gdacs).toHaveAttribute("target", "_blank");
    expect(gdacs.getAttribute("rel")).toContain("noopener");
    expect(within(drawer).getByText("Demo Freight Daily (Headline and link only)")).toBeInTheDocument();
    expect(within(drawer).getByText(/unconfirmed until an official source/)).toBeInTheDocument();
    const exposed = within(drawer).getByRole("list", { name: "Exposed sites and lanes" });
    expect(within(exposed).getByText("VNSGN")).toBeInTheDocument();
    expect(within(exposed).getByText("42 km from the affected area")).toBeInTheDocument();
    expect(within(exposed).getByText("Route crosses the affected area")).toBeInTheDocument();
    expect(within(drawer).getByText("1 supplier · 1 factory · 1 port · 4 lanes · 18,400 units inbound")).toBeInTheDocument();
    const updates = within(drawer).getByRole("list", { name: "Updates" });
    expect(within(updates).getAllByRole("listitem")[0]).toHaveTextContent("Tropical cyclone KAI-TAK, orange alert (update 4)");
    // Map: the existing NetworkMap block, footprint only (no WebGL in jsdom → its table view or skeleton).
    expect(drawer.querySelector("[data-block-kind='network_map'], [aria-busy='true'], [role='status']")).not.toBeNull();
    // raw_text (untrusted) is never rendered.
    expect(within(drawer).queryByText(/<script>/)).toBeNull();
    await waitFor(() => expect(calls.some((c) => c.url === `/api/alerts/${TYPHOON_ALERT_ID}/seen` && c.method === "POST")).toBe(true));
    await expectNoA11yViolations(drawer);
  });

  it("Open case: one POST with an Idempotency-Key, then the case opens", async () => {
    const user = userEvent.setup();
    const { calls } = renderInbox({ initial: TYPHOON_ALERT_ID });
    const drawer = await screen.findByRole("dialog", { name: "Tropical cyclone approaching Ho Chi Minh City" });
    await user.click(await within(drawer).findByRole("button", { name: "Open case" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/cases/case-from-alert"));
    const posts = calls.filter((c) => c.url.endsWith("/open-case"));
    expect(posts).toHaveLength(1);
    expect(posts[0]!.headers["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("Open case failure keeps the key, so a retry cannot open a second case", async () => {
    const user = userEvent.setup();
    let n = 0;
    const { calls } = renderInbox({
      initial: TYPHOON_ALERT_ID,
      routes: [(url) => (url.endsWith("/open-case") && n++ === 0 ? jsonResponse(503, { code: "unavailable", message: "try later" }) : undefined)],
    });
    const drawer = await screen.findByRole("dialog");
    await user.click(await within(drawer).findByRole("button", { name: "Open case" }));
    await screen.findAllByText("Couldn't open a case");
    await user.click(within(drawer).getByRole("button", { name: "Open case" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/cases/case-from-alert"));
    const keys = calls.filter((c) => c.url.endsWith("/open-case")).map((c) => c.headers["Idempotency-Key"]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("Dismiss and Snooze update the alert and close the drawer", async () => {
    const user = userEvent.setup();
    const { calls } = renderInbox({ initial: TYPHOON_ALERT_ID });
    const drawer = await screen.findByRole("dialog");
    await user.click(await within(drawer).findByRole("button", { name: "Dismiss" }));
    await screen.findAllByText("Alert dismissed");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Tropical cyclone approaching Ho Chi Minh City" })).toBeNull());
    expect(calls.some((c) => c.url === `/api/alerts/${TYPHOON_ALERT_ID}/dismiss` && c.method === "POST")).toBe(true);
    // Dismissed alerts leave "Needs attention".
    const list = screen.getByRole("list", { name: "Alerts" });
    expect(within(list).queryByRole("button", { name: "Tropical cyclone approaching Ho Chi Minh City" })).toBeNull();
    expect(window.location.search).toBe("");

    await user.click(within(list).getByRole("button", { name: /earthquake/ }));
    // The quake detail is not served → error state with retry, no actions.
    expect(await screen.findByText(/Couldn't load this alert/)).toBeInTheDocument();
  });

  it("read-only roles see the alert but no actions, and nothing is marked seen", async () => {
    const { calls } = renderInbox({ me: makeMe({ roles: ["compliance"] }), initial: TYPHOON_ALERT_ID });
    const drawer = await screen.findByRole("dialog");
    await within(drawer).findByText("GDACS (CC BY 4.0)");
    expect(within(drawer).queryByRole("button", { name: "Open case" })).toBeNull();
    expect(within(drawer).queryByRole("button", { name: "Dismiss" })).toBeNull();
    expect(within(drawer).getByText(/A planner can open a case/)).toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/seen"))).toBe(false);
  });

  it("an alert with a case links to it instead of opening another", async () => {
    renderInbox({
      initial: TYPHOON_ALERT_ID,
      routes: [(url) => (url === `/api/alerts/${TYPHOON_ALERT_ID}` ? jsonResponse(200, typhoonDetail({ alert: { status: "opened", case_id: "case-9" } })) : undefined)],
    });
    const drawer = await screen.findByRole("dialog");
    expect(await within(drawer).findByRole("link", { name: "Go to the case" })).toHaveAttribute("href", "/cases/case-9");
    expect(within(drawer).queryByRole("button", { name: "Open case" })).toBeNull();
  });

  it("a live alert appears at the top of the list", async () => {
    renderInbox();
    await screen.findByRole("list", { name: "Alerts" });
    act(() => FakeEventSource.latest().emit(customEvent("sc.alert.raised", alert({ id: "11111111-2222-5333-8444-555555555555", title: "New flood warning", updated_at: "2026-10-09T00:00:00Z", severity: "medium" }))));
    const rows = within(await screen.findByRole("list", { name: "Alerts" })).getAllByRole("button");
    await waitFor(() => expect(rows.length).toBeGreaterThan(0));
    expect(within(screen.getByRole("list", { name: "Alerts" })).getAllByRole("button")[0]).toHaveAccessibleName("New flood warning");
  });

  it("an invalid ?alert= id is ignored (no request built from it)", async () => {
    const { calls } = renderInbox({ initial: "../../api/demo/reset" });
    await screen.findByRole("list", { name: "Alerts" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls.every((c) => !c.url.includes(".."))).toBe(true);
  });
});

// ───────────────────────────── case thread ─────────────────────────────

describe("case thread: Opened from alert", () => {
  const origin = (): BlockEnvelope => structuredClone(getFixture("alert_origin")!.envelope);

  function stateWith(blocks: BlockEnvelope[]): CaseStreamState {
    const entries: Record<string, BlockEntry> = {};
    blocks.forEach((b, i) => (entries[b.id] = { envelope: b, runId: "run-sig", seq: i + 1, revision: 1, live: false }));
    const run: RunEntry = {
      id: "run-sig",
      status: "running",
      startedAt: 0,
      finishedAt: null,
      visited: [],
      active: null,
      agents: [],
      reasoning: [],
      messageIds: [],
      blockIds: blocks.map((b) => b.id),
      interruptIds: [],
      error: null,
    };
    return { ...initialCaseStreamState, runOrder: ["run-sig"], runs: { "run-sig": run }, blocks: entries, blockOrder: blocks.map((b) => b.id) };
  }

  it("the origin card heads the signal run's turn and is not repeated inline or on the canvas", () => {
    const kpi = structuredClone(getFixture("kpi_strip")!.envelope);
    const canvasOrigin = { ...origin(), id: "blk_alert_origin_canvas", placement: "canvas" as const };
    const state = stateWith([origin(), kpi, canvasOrigin]);
    const [turn] = buildTurns([], state);
    expect(turn!.origin?.envelope.id).toBe("blk_alert_origin_canvas");
    expect(turn!.inline.map((b) => b.envelope.id)).toEqual([kpi.id]);
    expect(canvasBlocks(state).result.map((b) => b.envelope.kind)).not.toContain("alert_origin");
  });

  it("TurnView renders the card where a user message would be", async () => {
    const state = stateWith([origin()]);
    const [turn] = buildTurns([], state);
    const { api } = fakeApi([]);
    const Wrapper = withSession(makeMe(), api);
    const { container } = render(
      <Wrapper>
        <TurnView
          turn={{ ...turn!, run: null }}
          index={0}
          interrupts={{}}
          assistantName="Keelson"
          gate={() => ({ disabled: false })}
          onAction={async () => {}}
          followUps={[]}
          onFollowUp={() => {}}
          followUpsDisabled={false}
        />
      </Wrapper>,
    );
    const section = screen.getByRole("region", { name: "Turn 1: opened from an alert" });
    const card = within(section).getByRole("region", { name: "Opened from alert" });
    expect(within(card).getByRole("link", { name: "View the alert" })).toHaveAttribute("href", `/alerts?alert=${TYPHOON_ALERT_ID}`);
    expect(within(card).getByText("DEMO")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });
});

// ───────────────────────────── presenter: demo packs ─────────────────────────────

describe("presenter: demo scenario packs", () => {
  function renderSwitcher(me: ReturnType<typeof makeMe>, packs: unknown = { items: [{ id: "typhoon-hcmc", title: "Typhoon approaching HCMC", description: "4 GDACS advisories + 1 news item", status: "idle" }, { id: "BAD/ID", title: "x" }] }) {
    const { api, calls } = fakeApi([
      (url) => (url === "/api/demo/personas" ? jsonResponse(200, [{ username: "priya", name: "Priya Shah", role_label: "Planner", initials: "PS" }]) : undefined),
      (url) => (url === "/api/demo/packs" ? jsonResponse(200, packs) : undefined),
      (url) => (url === "/api/demo/packs/typhoon-hcmc/start" ? jsonResponse(200, { ok: true, status: "running", step: 1, steps_total: 5 }) : undefined),
      (url) => (url === "/api/demo/packs/typhoon-hcmc/advance" ? jsonResponse(200, { ok: true, status: "running", step: 2, steps_total: 5 }) : undefined),
    ]);
    const Wrapper = withSession(me, api);
    const utils = render(
      <Wrapper>
        <Toaster />
        <PersonaSwitcher reload={() => {}} />
      </Wrapper>,
    );
    return { ...utils, calls };
  }

  it("describes progress in words", () => {
    expect(packProgress({ status: "running", step: 2, steps_total: 5 })).toBe("Running · step 2 of 5");
    expect(packProgress({ step: 3 })).toBe("step 3");
    expect(packProgress({})).toBeNull();
  });

  it("a presenter starts and advances a pack from the persona menu", async () => {
    const user = userEvent.setup();
    const { calls } = renderSwitcher(makeMe({ roles: ["demo-presenter"] }));
    await user.click(screen.getByRole("button", { name: /demo persona/i }));
    await user.click(await screen.findByRole("menuitem", { name: /Demo scenarios/ }));
    const dialog = await screen.findByRole("dialog", { name: "Demo scenarios" });
    const list = await within(dialog).findByRole("list", { name: "Demo scenarios" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1); // the malformed pack id is dropped
    await user.click(within(dialog).getByRole("button", { name: "Start: Typhoon approaching HCMC" }));
    await screen.findByText('Started "Typhoon approaching HCMC"');
    expect(within(dialog).getByText("Running · step 1 of 5")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Next step: Typhoon approaching HCMC" }));
    await waitFor(() => expect(within(dialog).getByText("Running · step 2 of 5")).toBeInTheDocument());
    const posts = calls.filter((c) => c.url.startsWith("/api/demo/packs/"));
    expect(posts.map((c) => [c.url, c.method, c.headers["X-CSRF-Token"]])).toEqual([
      ["/api/demo/packs/typhoon-hcmc/start", "POST", "csrf-test"],
      ["/api/demo/packs/typhoon-hcmc/advance", "POST", "csrf-test"],
    ]);
    await expectNoA11yViolations(dialog);
  });

  it("an empty / unavailable pack list says so", async () => {
    const user = userEvent.setup();
    renderSwitcher(makeMe({ roles: ["platform-admin"] }), []);
    await user.click(screen.getByRole("button", { name: /demo persona/i }));
    await user.click(await screen.findByRole("menuitem", { name: /Demo scenarios/ }));
    expect(await screen.findByText("No demo scenarios are installed.")).toBeInTheDocument();
  });

  it("is not offered to other personas", async () => {
    const user = userEvent.setup();
    renderSwitcher(makeMe({ roles: ["planner"] }));
    await user.click(screen.getByRole("button", { name: /demo persona/i }));
    await screen.findByRole("menuitem", { name: /Priya Shah/ });
    expect(screen.queryByRole("menuitem", { name: /Demo scenarios/ })).toBeNull();
  });
});

// ───────────────────────────── review regressions (engine wire shapes, DEMO) ─────────────────────────────

describe("M6 review regressions", () => {
  const T0 = Date.parse("2026-10-08T06:05:00Z");
  const raised = (a: Alert): AlertUpdate => ({ name: "sc.alert.raised", alert: a, alertId: a.id, newCount: null });

  it("parses the engine's sc.alert.count {new}, removed {alert_id} and case_opened events", () => {
    expect(parseAlertEvent(customEvent("sc.alert.count", { new: 7 }))).toMatchObject({ name: "sc.alert.count", alert: null, newCount: 7 });
    expect(parseAlertEvent(customEvent("sc.alert.count", { new: -1 }))).toMatchObject({ newCount: null });
    expect(parseAlertEvent(customEvent("sc.alert.raised", { alert: alert(), renotify: true }))).toMatchObject({ alert: { id: TYPHOON_ALERT_ID }, alertId: TYPHOON_ALERT_ID });
    expect(parseAlertEvent(customEvent("sc.alert.removed", { alert_id: QUAKE_ALERT_ID }))).toMatchObject({ alertId: QUAKE_ALERT_ID, removed: true });
    expect(parseAlertEvent(customEvent("sc.alert.removed", { alert_id: "../x" }))).toMatchObject({ alertId: null, removed: false });
    expect(parseAlertEvent(customEvent("sc.alert.case_opened", { alert_id: TYPHOON_ALERT_ID, case_id: "case-7", run_id: "r", auto: true }))).toMatchObject({
      alertId: TYPHOON_ALERT_ID,
      caseId: "case-7",
    });
  });

  it("DEMO alerts (SimClock time, days old) still toast; real stale ones do not; an auto-opened draft case toasts", () => {
    const simOld = alert({ updated_at: "2026-10-05T18:00:00Z", simulated: true });
    expect(shouldToast(raised(simOld), new Set(), T0)).toBe(true);
    expect(shouldToast(raised({ ...simOld, simulated: false }), new Set(), T0)).toBe(false);
    // alert_and_draft_case: the engine opens the case before publishing `raised` (status "opened").
    expect(shouldToast(raised(alert({ status: "opened", case_id: "case-1" })), new Set(), T0)).toBe(true);
    expect(shouldToast(raised(alert({ status: "dismissed" })), new Set(), T0)).toBe(false);
  });

  it("the pill follows sc.alert.count and drops removed / case-opened alerts", async () => {
    const { api } = fakeApi([(url) => (url.startsWith("/api/alerts?status=new") ? jsonResponse(200, { items: [alert(), alert({ id: NEWS_ALERT_ID })] }) : undefined)]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <AlertChannelProvider createEventSource={FakeEventSource.factory}>
          <NewAlertsPill />
        </AlertChannelProvider>
      </Wrapper>,
    );
    await screen.findByText("2 new alerts");
    const es = FakeEventSource.latest();
    act(() => es.emit(customEvent("sc.alert.count", { new: 5 })));
    await screen.findByText("5 new alerts");
    act(() => es.emit(customEvent("sc.alert.removed", { alert_id: NEWS_ALERT_ID })));
    await screen.findByText("1 new alert");
    act(() => es.emit(customEvent("sc.alert.case_opened", { alert_id: TYPHOON_ALERT_ID, case_id: "case-1", run_id: null, auto: true })));
    await screen.findByText("No new alerts");
  });

  it("a demo pack reset + replay (same uuid5 alert id) toasts again", async () => {
    const { api } = fakeApi([(url) => (url.startsWith("/api/alerts?status=new") ? jsonResponse(200, { items: [] }) : undefined)]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <Toaster />
        <AlertChannelProvider createEventSource={FakeEventSource.factory}>
          <NewAlertsPill />
        </AlertChannelProvider>
      </Wrapper>,
    );
    await screen.findByText("No new alerts");
    const es = FakeEventSource.latest();
    const demo = alert({ updated_at: "2026-10-05T18:00:00Z" });
    act(() => es.emit(customEvent("sc.alert.raised", { alert: demo, renotify: false })));
    expect(await screen.findAllByRole("button", { name: "View alert", hidden: true })).toHaveLength(1);
    expect(window.sessionStorage.getItem("keelson.alerts.notified")).toContain(TYPHOON_ALERT_ID);
    act(() => es.emit(customEvent("sc.alert.removed", { alert_id: TYPHOON_ALERT_ID })));
    expect(window.sessionStorage.getItem("keelson.alerts.notified")).not.toContain(TYPHOON_ALERT_ID);
    act(() => es.emit(customEvent("sc.alert.raised", { alert: demo, renotify: false })));
    await waitFor(() => expect(screen.getAllByRole("button", { name: "View alert", hidden: true })).toHaveLength(2));
  });

  it("inbox rows: removed alerts leave, case_opened links the case", () => {
    const items = [alert(), quakeAlert];
    expect(applyUpdate(items, { name: "sc.alert.removed", alert: null, alertId: QUAKE_ALERT_ID, newCount: null, removed: true }).map((a) => a.id)).toEqual([TYPHOON_ALERT_ID]);
    const opened = applyUpdate(items, { name: "sc.alert.case_opened", alert: null, alertId: TYPHOON_ALERT_ID, newCount: null, caseId: "case-1" });
    expect(opened[0]).toMatchObject({ status: "opened", case_id: "case-1" });
    expect(applyUpdate(items, { name: "sc.alert.count", alert: null, alertId: null, newCount: 3 })).toBe(items);
  });

  it("a toast's View alert opens the drawer in place while the inbox is on screen", async () => {
    const user = userEvent.setup();
    const { api } = fakeApi([
      (url) => (url.startsWith("/api/alerts?") ? jsonResponse(200, { items: [quakeAlert] }) : undefined),
      (url) => (url === `/api/alerts/${TYPHOON_ALERT_ID}` ? jsonResponse(200, typhoonDetail()) : undefined),
      (url) => (url.endsWith("/seen") ? jsonResponse(200, alert({ status: "seen" })) : undefined),
    ]);
    const Wrapper = withSession(makeMe(), api);
    render(
      <Wrapper>
        <Toaster />
        <AlertChannelProvider createEventSource={FakeEventSource.factory}>
          <AlertsInbox canWrite />
        </AlertChannelProvider>
      </Wrapper>,
    );
    await screen.findByRole("list", { name: "Alerts" });
    act(() => FakeEventSource.latest().emit(customEvent("sc.alert.raised", { alert: alert({ updated_at: "2026-10-05T12:00:00Z" }), renotify: false })));
    await user.click(await screen.findByRole("button", { name: "View alert", hidden: true }));
    expect(await screen.findByRole("dialog", { name: "Tropical cyclone approaching Ho Chi Minh City" })).toBeInTheDocument();
    expect(nav.push).not.toHaveBeenCalled();
    expect(window.location.search).toBe(`?alert=${TYPHOON_ALERT_ID}`);
  });

  it("the list limit stays within the engine's 1..200", async () => {
    const { api, calls } = fakeApi([(url) => (url.startsWith("/api/alerts?") ? jsonResponse(200, { items: [] }) : undefined)]);
    const alerts = new AlertsApi(api);
    await alerts.list({ limit: 1000 });
    await alerts.list({ limit: Number.NaN });
    await alerts.list({ limit: 0 });
    expect(calls.map((c) => c.url)).toEqual([`/api/alerts?limit=${ALERT_LIST_MAX}`, "/api/alerts?limit=100", "/api/alerts?limit=1"]);
  });

  it("source links never point at internal hosts", () => {
    for (const bad of [
      "http://signal-hub-api:8300/demo-feeds/x",
      "http://localhost:3000/",
      "http://127.0.0.1/",
      "http://0x7f.1/",
      "http://[::1]/",
      "https://intranet.local/x",
      "http://metadata.internal/",
    ]) {
      expect(safeSourceUrl(bad)).toBeNull();
    }
    expect(safeSourceUrl("https://demo-freight-daily.example/2026/10/05/a")).toBe("https://demo-freight-daily.example/2026/10/05/a");
  });

  it("demo packs: the Signal Hub's own list and state shapes are understood", () => {
    expect(
      demoPacksSchema.parse({
        packs: [
          { id: "typhoon-hcmc", title: "Typhoon", description: "d", start_at: "2026-10-05T00:00:00Z", steps: 5 },
          { id: "broken", error: "bad yaml" },
        ],
      }),
    ).toEqual([{ id: "typhoon-hcmc", title: "Typhoon", description: "d", step: null, steps_total: 5 }]);
    const state = demoPackResultSchema.parse({ status: "running", steps_total: 5, steps_applied: 2, next_step: { index: 2, note: "Advisory 3" }, log: [] });
    expect(state).toMatchObject({ status: "running", step: 2, steps_total: 5, note: "Advisory 3" });
    expect(packProgress(state)).toBe("Running · step 2 of 5");
    expect(demoPackResultSchema.parse({})).toMatchObject({ step: null, note: null });
  });

  it("demo packs: without a list route the presenter enters a scenario id (slugs only)", async () => {
    const user = userEvent.setup();
    const { api, calls } = fakeApi([
      (url) => (url === "/api/demo/personas" ? jsonResponse(200, []) : undefined),
      (url) => (url === "/api/demo/packs/typhoon-hcmc/start" ? jsonResponse(200, { status: "running", steps_total: 5, steps_applied: 1 }) : undefined),
    ]);
    const Wrapper = withSession(makeMe({ roles: ["demo-presenter"] }), api);
    render(
      <Wrapper>
        <Toaster />
        <PersonaSwitcher reload={() => {}} />
      </Wrapper>,
    );
    await user.click(screen.getByRole("button", { name: /demo persona/i }));
    await user.click(await screen.findByRole("menuitem", { name: /Demo scenarios/ }));
    const dialog = await screen.findByRole("dialog", { name: "Demo scenarios" });
    const input = await within(dialog).findByRole("textbox", { name: "Scenario id" });
    await user.type(input, "../reset");
    await user.click(within(dialog).getByRole("button", { name: "Use scenario" }));
    expect(await within(dialog).findByText(/lowercase letters, digits and hyphens/)).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "typhoon-hcmc");
    await user.click(within(dialog).getByRole("button", { name: "Use scenario" }));
    await user.click(await within(dialog).findByRole("button", { name: "Start: typhoon-hcmc" }));
    await waitFor(() => expect(within(dialog).getByText("Running · step 1 of 5")).toBeInTheDocument());
    expect(calls.filter((c) => c.method === "POST").map((c) => c.url)).toEqual(["/api/demo/packs/typhoon-hcmc/start"]);
    await expectNoA11yViolations(dialog);
  });
});
