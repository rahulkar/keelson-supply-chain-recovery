/**
 * M8 signal feeds in the Signals admin (`docs/contracts/124-m8-signal-feeds.md` §4): the Sources
 * table shows the source type and provenance, licence + attribution + an https-only terms link,
 * coverage and the OFAC awareness disclaimer; opt-in sources start off and subscribe with
 * `{enabled: true}`; a source this installation does not poll cannot be switched on.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SignalsPage } from "@/components/admin/SignalsPage";
import { cadenceOptions, provenanceLabel, safeTermsUrl, signalSourceListSchema, sourceSwitchNote } from "@/lib/admin/signals";
import { expectNoA11yViolations } from "./a11y";
import * as F from "./fixtures/m8-feeds";
import { fakeApi, jsonResponse, makeMe, withSession, type Route } from "./session";

vi.mock("next/navigation", () => ({ usePathname: () => "/admin/signals", useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }) }));
vi.mock("@keelson/ui", async (orig) => {
  const mod = await orig<typeof import("@keelson/ui")>();
  const noop = () => "t";
  return { ...mod, toast: Object.assign(noop, { success: noop, error: noop, dismiss: () => {} }) };
});

vi.setConfig({ testTimeout: 30_000 });

const SAM = () => makeMe({ username: "sam", name: "Sam Okafor", roles: ["tenant-admin"], tenants: ["demo-retail"] });

let sources: Record<string, unknown>;

const routes: Route = (url, init) => {
  const method = init.method ?? "GET";
  const path = url.split("?")[0]!;
  if (method === "GET" && path === "/api/admin/signals/sources") return jsonResponse(200, sources);
  const m = /^\/api\/admin\/signals\/sources\/([^/]+)$/.exec(path);
  if (m && (method === "PUT" || method === "PATCH")) {
    const body = JSON.parse(String(init.body ?? "{}")) as Record<string, unknown>;
    return jsonResponse(200, { id: decodeURIComponent(m[1]!), enabled: body["enabled"] ?? false, cadence_s: body["cadence_s"] ?? null, hub_synced: true, hub_error: null });
  }
  if (method === "GET" && path === "/api/admin/signals/network") return jsonResponse(200, { nodes: {} });
  if (method === "GET" && path === "/api/admin/signals/watch-regions") return jsonResponse(200, { items: [] });
  if (method === "GET" && path === "/api/admin/signals/alert-rules") return jsonResponse(200, { custom: false, rules: [], defaults: [] });
  if (path.startsWith("/api/demo/packs")) return jsonResponse(200, { packs: [] });
  return undefined;
};

function renderPage() {
  const { api, calls } = fakeApi([routes]);
  const Wrapper = withSession(SAM(), api);
  const utils = render(
    <Wrapper>
      <main>
        <SignalsPage />
      </main>
    </Wrapper>,
  );
  return { ...utils, calls };
}

const row = (table: HTMLElement, name: RegExp) => within(table).getByRole("rowheader", { name }).closest("tr")!;

beforeEach(() => {
  sources = F.feedSources();
});

describe("signal feeds (lib)", () => {
  it("parses the M8 catalogue facts and keeps M7 rows working", () => {
    const list = signalSourceListSchema.parse(F.feedSources());
    expect(list.sources.map((s) => s.id)).toEqual(["nhc", "ofac", "gdelt", "meteoalarm.netherlands", "gdacs"]);
    const [nhc, , gdelt, meteo, gdacs] = list.sources;
    expect(nhc).toMatchObject({ kind: "hazard", provenance: "fact", licence: "Public domain (NOAA/NWS)", opt_in: false, polled: true });
    expect(gdelt).toMatchObject({ opt_in: true, enabled: false, paused: true, authority_bp: 4000 });
    expect(gdacs!.provenance).toBeUndefined();
    expect(provenanceLabel(nhc!)).toBe("Fact");
    expect(provenanceLabel(gdelt!)).toBe("Inference · low authority");
    expect(provenanceLabel({ provenance: "inference", authority_bp: 7000 })).toBe("Inference");
    expect(provenanceLabel(gdacs!)).toBeNull();
    expect(sourceSwitchNote(gdelt!)).toBe("Starts polling when a workspace subscribes");
    expect(sourceSwitchNote(meteo!)).toMatch(/Not enabled on this installation/);
    expect(sourceSwitchNote(nhc!)).toBeNull();
  });

  it("terms links: https with a plain host only", () => {
    expect(safeTermsUrl("https://www.weather.gov/disclaimer")).toBe("https://www.weather.gov/disclaimer");
    for (const bad of ["javascript:alert(1)", "http://www.meteoalarm.org/x", "https://user:pw@evil.example/", "https://evil.example", "data:text/html,x", "https://a.example/ x", null, undefined, ""]) {
      expect(safeTermsUrl(bad)).toBeNull();
    }
  });

  it("cadence choices stay inside the source's own limits; a stored value is still offered", () => {
    const labels = (s: Parameters<typeof cadenceOptions>[0]) => cadenceOptions(s).map((o) => o.label);
    const ofac = labels({ cadence_s: null, default_cadence_s: 3600, min_cadence_s: 1800, max_cadence_s: 86_400 });
    expect(ofac[0]).toBe("Default (every 1 h)");
    expect(ofac.slice(1)).toEqual(["every 30 min", "every 1 h", "every 6 h", "every 12 h", "every 24 h"]);
    expect(labels({ cadence_s: 900, default_cadence_s: 3600, min_cadence_s: 1800, max_cadence_s: 86_400 })).toContain("every 15 min");
    // no limits reported (an older engine or hub): every choice, as in M7
    expect(labels({ cadence_s: null, default_cadence_s: 900 })).toContain("every 1 min");
  });
});

describe("Sources table (M8)", () => {
  it("shows type, provenance, licence, attribution, terms, coverage and the OFAC disclaimer; axe", async () => {
    const { container } = renderPage();
    const table = await screen.findByRole("table", { name: "Signal sources" });
    const nhc = row(table, /NOAA National Hurricane Center/);
    expect(within(nhc).getByText("Hazard")).toBeInTheDocument();
    expect(within(nhc).getByText("Fact")).toBeInTheDocument();
    expect(nhc).toHaveTextContent("Public domain (NOAA/NWS)");
    expect(nhc).toHaveTextContent("Source: NOAA National Hurricane Center");
    expect(nhc).toHaveTextContent("Atlantic, East and Central Pacific tropical cyclones");
    const terms = within(nhc).getByRole("link", { name: /Terms of NOAA National Hurricane Center/ });
    expect(terms).toHaveAttribute("href", "https://www.weather.gov/disclaimer");
    expect(terms).toHaveAttribute("target", "_blank");
    expect(terms).toHaveAttribute("rel", "noopener noreferrer");
    const ofac = row(table, /OFAC SDN list/);
    expect(within(ofac).getByText("Regulatory")).toBeInTheDocument();
    const ofacCadences = [...within(ofac).getByLabelText("Polling cadence of U.S. Treasury OFAC SDN list").querySelectorAll("option")];
    expect(ofacCadences.slice(1).map((o) => Number(o.getAttribute("value")))).toEqual([1800, 3600, 21_600, 43_200, 86_400]);
    expect(ofac).toHaveTextContent("not a sanctions-screening service");
    const gdelt = row(table, /GDELT Project/);
    expect(within(gdelt).getByText("Inference · low authority")).toBeInTheDocument();
    expect(within(gdelt).getByText("Opt-in")).toBeInTheDocument();
    expect(within(gdelt).queryByRole("link")).toBeNull(); // the javascript: URL is never rendered
    expect(container.innerHTML).not.toContain("javascript:");
    expect(within(row(table, /MeteoAlarm warnings: Netherlands/)).queryByRole("link")).toBeNull(); // http
    const gdacs = row(table, /GDACS disaster alerts/);
    expect(gdacs).toHaveTextContent("CC BY 4.0");
    await expectNoA11yViolations(container);
  });

  it("an opt-in source is off and subscribing PUTs {enabled: true}; its cadence waits for the subscription", async () => {
    const user = userEvent.setup();
    const { calls } = renderPage();
    const table = await screen.findByRole("table", { name: "Signal sources" });
    const gdelt = row(table, /GDELT Project/);
    const sw = within(gdelt).getByRole("switch", { name: "Subscribe to GDELT Project 2.0 events" });
    expect(sw).not.toBeChecked();
    expect(gdelt).toHaveTextContent("Starts polling when a workspace subscribes");
    expect(within(gdelt).getByLabelText("Polling cadence of GDELT Project 2.0 events")).toBeDisabled();
    await user.click(sw);
    await waitFor(() => expect(calls.some((c) => c.url === "/api/admin/signals/sources/gdelt")).toBe(true));
    expect(calls.find((c) => c.url === "/api/admin/signals/sources/gdelt")!.body).toEqual({ enabled: true });
    await waitFor(() => expect(within(row(table, /GDELT Project/)).getByRole("switch", { name: /Unsubscribe from GDELT/ })).toBeChecked());
  });

  it("a source this installation does not poll cannot be switched on", async () => {
    renderPage();
    const table = await screen.findByRole("table", { name: "Signal sources" });
    const meteo = row(table, /MeteoAlarm warnings: Netherlands/);
    expect(within(meteo).getByRole("switch", { name: /Subscribe to MeteoAlarm warnings/ })).toBeDisabled();
    expect(meteo).toHaveTextContent("Not enabled on this installation (platform admin: SIGNAL_ADAPTERS)");
    expect(within(meteo).getByText("Weather")).toBeInTheDocument();
  });
});
