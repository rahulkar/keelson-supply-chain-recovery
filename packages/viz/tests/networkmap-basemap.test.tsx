import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import manifestFixture from "./fixtures/basemap-manifest.json";

type Seen = { kind: string; manifest: unknown } | undefined;

const SIMPLIFIED = /Showing a simplified background map/;
const PLAIN = /background map couldn.t be loaded/;
const defaultFetch = globalThis.fetch;
let seen: Seen[] = [];

/** NetworkMap with WebGL and a stand-in canvas that reports its base map and can fail it. */
type Viz = typeof import("../src");

async function renderMap(wrap: (children: ReactNode, viz: Viz) => ReactNode = (c) => c) {
  vi.resetModules();
  vi.doMock("../src/map/webgl", () => ({ supportsWebGL2: () => true }));
  vi.doMock("../src/map/MapCanvas", () => ({
    default: (p: { basemap?: { kind: string; installed: { manifest: unknown } | null }; onBaseMapError?: () => void }) => {
      seen.push(p.basemap ? { kind: p.basemap.kind, manifest: p.basemap.installed?.manifest ?? null } : undefined);
      return (
        <div data-testid="map-canvas">
          <button type="button" onClick={() => p.onBaseMapError?.()}>
            fail base map
          </button>
        </div>
      );
    },
  }));
  const viz = await import("../src");
  const fixtures = await import("../src/fixtures");
  const f = fixtures.getFixture("network_map")!;
  const utils = render(<>{wrap(<viz.BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} density="canvas" />, viz)}</>);
  return { ...utils, viz };
}

const mapRegion = () => screen.getByRole("group", { name: /^Map of 16 sites/ });
const basemapAttr = () => mapRegion().getAttribute("data-map-basemap");

function serveManifest(status: number, body?: unknown) {
  const fn = vi.fn<typeof fetch>(async () => new Response(body === undefined ? null : JSON.stringify(body), { status }));
  globalThis.fetch = fn;
  return fn;
}

beforeEach(() => {
  seen = [];
});
afterEach(() => {
  globalThis.fetch = defaultFetch;
  vi.doUnmock("../src/map/webgl");
  vi.doUnmock("../src/map/MapCanvas");
});

describe("NetworkMap base map fallback chain", () => {
  it("auto with an installed base map draws Protomaps, then falls back step by step with the right notice", async () => {
    const fetchMock = serveManifest(200, manifestFixture);
    const user = userEvent.setup();
    await renderMap();
    expect(await screen.findByTestId("map-canvas")).toBeInTheDocument();
    expect(basemapAttr()).toBe("protomaps");
    expect(seen.at(-1)).toEqual({ kind: "protomaps", manifest: manifestFixture });
    expect(String(fetchMock.mock.calls[0]![0])).toMatch(/\/basemap\/manifest\.json$/);
    expect(screen.queryByText(SIMPLIFIED)).toBeNull();

    // protomaps → openfreemap is silent (both are full maps)
    await user.click(screen.getByRole("button", { name: "fail base map" }));
    await waitFor(() => expect(basemapAttr()).toBe("openfreemap"));
    expect(screen.queryByText(SIMPLIFIED)).toBeNull();
    expect(screen.queryByText(PLAIN)).toBeNull();

    await user.click(screen.getByRole("button", { name: "fail base map" }));
    await waitFor(() => expect(basemapAttr()).toBe("offline"));
    expect(screen.getByText(SIMPLIFIED)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "fail base map" }));
    await waitFor(() => expect(basemapAttr()).toBe("plain"));
    expect(screen.getByText(PLAIN)).toBeInTheDocument();
    expect(screen.queryByText(SIMPLIFIED)).toBeNull();
    // plain is final
    await user.click(screen.getByRole("button", { name: "fail base map" }));
    expect(basemapAttr()).toBe("plain");
    expect(seen.map((s) => s?.kind).filter((k, i, a) => k !== a[i - 1])).toEqual(["protomaps", "openfreemap", "offline", "plain"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // the table still lists every site
    await user.click(screen.getByRole("button", { name: "View as table" }));
    expect(within(screen.getByRole("table", { name: /^Sites/ })).getAllByRole("row")).toHaveLength(17);
  });

  it("auto without a base map (404 / 204) starts at OpenFreeMap", async () => {
    serveManifest(204);
    await renderMap();
    await waitFor(() => expect(basemapAttr()).toBe("openfreemap"));
    // the canvas is a lazy chunk: it renders a moment after the region knows its kind
    await waitFor(() => expect(seen.at(-1)).toEqual({ kind: "openfreemap", manifest: null }));
  });

  it("an invalid manifest counts as none", async () => {
    serveManifest(200, { ...manifestFixture, attribution: "<img src=x onerror=alert(1)>" });
    await renderMap();
    await waitFor(() => expect(basemapAttr()).toBe("openfreemap"));
  });

  it("shows the skeleton while the manifest is being resolved", async () => {
    globalThis.fetch = vi.fn<typeof fetch>(() => new Promise(() => {}));
    await renderMap();
    await waitFor(() => expect(screen.getByRole("status", { name: /Loading map/ })).toBeInTheDocument());
    expect(screen.queryByTestId("map-canvas")).toBeNull();
    expect(mapRegion().hasAttribute("data-map-basemap")).toBe(false);
  });

  it("mode protomaps without a base map draws the outline and never OpenFreeMap", async () => {
    serveManifest(404);
    await renderMap((c, viz) => <viz.NetworkMapSettingsProvider basemap={{ mode: "protomaps" }}>{c}</viz.NetworkMapSettingsProvider>);
    await waitFor(() => expect(basemapAttr()).toBe("offline"));
    expect(screen.getByText(SIMPLIFIED)).toBeInTheDocument();
    expect(seen.map((s) => s?.kind)).not.toContain("openfreemap");
  });

  it("mode offline draws the outline without asking for a manifest and without a notice", async () => {
    const fetchMock = serveManifest(200, manifestFixture);
    await renderMap((c, viz) => <viz.NetworkMapSettingsProvider basemap={{ mode: "offline" }}>{c}</viz.NetworkMapSettingsProvider>);
    await waitFor(() => expect(basemapAttr()).toBe("offline"));
    expect(screen.queryByText(SIMPLIFIED)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("the provider overrides configureNetworkMap for its subtree", async () => {
    const viz = await import("../src");
    viz.configureNetworkMap({ basemap: { mode: "openfreemap", manifestUrl: "/maps/manifest.json" } });
    try {
      render(
        <>
          <Probe viz={viz} id="outside" />
          <viz.NetworkMapSettingsProvider basemap={{ mode: "offline" }}>
            <Probe viz={viz} id="inside" />
          </viz.NetworkMapSettingsProvider>
        </>,
      );
      expect(screen.getByTestId("outside")).toHaveTextContent("openfreemap /maps/manifest.json");
      expect(screen.getByTestId("inside")).toHaveTextContent("offline /maps/manifest.json");
    } finally {
      viz.configureNetworkMap(viz.DEFAULT_NETWORK_MAP_CONFIG);
    }
  });

  it("asks for no manifest when the map is not drawn (no WebGL)", async () => {
    const fetchMock = serveManifest(200, manifestFixture);
    vi.resetModules();
    vi.doMock("../src/map/webgl", () => ({ supportsWebGL2: () => false }));
    const viz = await import("../src");
    const fixtures = await import("../src/fixtures");
    const f = fixtures.getFixture("network_map")!;
    render(<viz.BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} density="canvas" />);
    expect(await screen.findByText(/WebGL is unavailable/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

/** Reads the effective settings through the public hook. */
function Probe({ viz, id }: { viz: Viz; id: string }) {
  const s = viz.useNetworkMapSettings();
  return <span data-testid={id}>{`${s.mode} ${s.manifestUrl}`}</span>;
}
