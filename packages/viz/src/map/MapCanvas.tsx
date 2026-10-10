"use client";

/**
 * The WebGL part of the network map: react-map-gl 8 (MapLibre 6) base map + a deck.gl 9
 * `MapLibreOverlay` (sites, lanes, proposed arcs, disruption footprint). Loaded on demand by
 * `NetworkMap` only when WebGL2 is available; everything it draws is also in "View as table".
 * The base map is one kind of the fallback chain (docs/contracts/127-m8-basemap.md): self-hosted
 * Protomaps, OpenFreeMap, the committed Natural Earth outline or a plain background.
 */
import "maplibre-gl/dist/maplibre-gl.css";
import { MapLibreOverlay, type MapLibreOverlayProps } from "@deck.gl/maplibre";
import { ArcLayer, GeoJsonLayer, PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import type { MapNode } from "@keelson/contracts";
import { hexColor, rgba, type ThemeName } from "@keelson/tokens";
import * as maplibre from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import { Map as MapView, NavigationControl, useControl } from "react-map-gl/maplibre";
import { formatValue } from "../format";
import { statusLabel, statusRgba } from "../lib/status";
import { maxZoomFor, type BasemapKind, type InstalledBasemap } from "./basemap";
import { networkMapConfig } from "./config";
import {
  bboxToBounds,
  linkColorRole,
  linkKindLabels,
  nodeKindLabels,
  nodePosition,
  nodeRadius,
  type FootprintFeatureGeoJson,
  type ArcLeg,
  type MapModel,
  type PathLeg,
} from "./model";
import { offlineStyle } from "./offline";
import { ensurePmtilesProtocol } from "./pmtilesProtocol";
import { protomapsStyle } from "./protomaps";

let workerUrlSet: string | null = null;

/** MapLibre loads its worker from our own origin (`public/maplibre/`), never from a blob (CSP). */
function ensureWorkerUrl(): void {
  const url = networkMapConfig().workerUrl;
  if (workerUrlSet === url) return;
  maplibre.setWorkerUrl(url);
  workerUrlSet = url;
}

/** Plain background used when the base map style cannot be loaded (offline, blocked). */
export function fallbackStyle(theme: ThemeName): maplibre.StyleSpecification {
  return {
    version: 8,
    sources: {},
    layers: [{ id: "background", type: "background", paint: { "background-color": hexColor("surface-2", theme) } }],
  };
}

/**
 * The MapLibre style for a base map kind: a style object built in code (Protomaps from the
 * installed manifest, the offline outline, the plain background) or the hosted style URL
 * (OpenFreeMap). `protomaps` without an installed manifest cannot happen in the chain; it draws
 * the outline rather than nothing.
 */
export function basemapStyle(kind: BasemapKind, theme: ThemeName, installed: InstalledBasemap | null): maplibre.StyleSpecification | string {
  switch (kind) {
    case "protomaps":
      return installed ? protomapsStyle(theme, installed) : offlineStyle(theme);
    case "openfreemap":
      return networkMapConfig().styleUrls[theme];
    case "offline":
      return offlineStyle(theme);
    case "plain":
      return fallbackStyle(theme);
  }
}

/**
 * Whether a MapLibre `error` event means the base map cannot be shown: only before the map has
 * loaded, and not for one tile that failed (MapLibre retries / shows the neighbours).
 */
export function isBaseMapFailure(event: object, mapLoaded: boolean): boolean {
  return !mapLoaded && !("tile" in event && (event as { tile?: unknown }).tile);
}

function DeckOverlay(props: MapLibreOverlayProps) {
  const overlay = useControl(() => new MapLibreOverlay(props));
  useEffect(() => {
    overlay.setProps(props);
  }, [overlay, props]);
  return null;
}

type Picked = MapNode | PathLeg | ArcLeg | FootprintFeatureGeoJson;

function isNode(o: Picked): o is MapNode {
  return "lat_e6" in o;
}
function isLeg(o: Picked): o is PathLeg | ArcLeg {
  return "link" in o;
}

export type MapCanvasProps = {
  model: MapModel;
  bbox: [number, number, number, number];
  theme: ThemeName;
  reducedMotion: boolean;
  /** Node id → label (lane tooltips). */
  labels: ReadonlyMap<string, string>;
  /** Option id → title (lane tooltips). */
  optionTitles: ReadonlyMap<string, string>;
  height: number;
  /** Base map to draw (NetworkMap's fallback chain); default: the hosted OpenFreeMap style. */
  basemap?: { kind: BasemapKind; installed: InstalledBasemap | null } | undefined;
  /**
   * The base map could not be loaded (style, TileJSON / archive, sprite, worker). The canvas shows
   * the plain background meanwhile; NetworkMap remounts it with the next kind of the chain.
   */
  onBaseMapError?: (() => void) | undefined;
};

export default function MapCanvas({
  model,
  bbox,
  theme,
  reducedMotion,
  labels,
  optionTitles,
  height,
  basemap,
  onBaseMapError,
}: MapCanvasProps) {
  ensureWorkerUrl();
  const kind = basemap?.kind ?? "openfreemap";
  const installed = basemap?.installed ?? null;
  if (kind === "protomaps") ensurePmtilesProtocol(maplibre);
  const loaded = useRef(false);
  const [styleFailed, setStyleFailed] = useState(false);
  // Memoised per theme: a theme switch hands MapLibre a new style object and it diffs (setStyle).
  const style = useMemo(() => basemapStyle(kind, theme, installed), [kind, theme, installed]);
  const mapStyle = styleFailed ? fallbackStyle(theme) : style;
  const maxZoom = maxZoomFor(kind, installed?.manifest ?? null);
  const initialViewState = useMemo(() => ({ bounds: bboxToBounds(bbox), fitBoundsOptions: { padding: 32, maxZoom: 7 } }), [bbox]);

  const layers = useMemo(() => {
    const surface = rgba("surface", theme);
    return [
      new GeoJsonLayer<FootprintFeatureGeoJson["properties"]>({
        id: "footprint",
        data: model.footprint ?? { type: "FeatureCollection", features: [] },
        filled: true,
        stroked: true,
        getFillColor: (f) => statusRgba(f.properties.status, theme, 46),
        getLineColor: (f) => statusRgba(f.properties.status, theme, 180),
        lineWidthUnits: "pixels",
        getLineWidth: 1.5,
        pickable: true,
        updateTriggers: { getFillColor: theme, getLineColor: theme },
      }),
      new PathLayer<PathLeg>({
        id: "lanes",
        data: model.legs.paths,
        getPath: (leg) => leg.path,
        getColor: (leg) => rgba(linkColorRole(leg.link), theme),
        getWidth: (leg) => (leg.link.status === "disrupted" ? 4 : 2.5),
        widthUnits: "pixels",
        capRounded: true,
        jointRounded: true,
        pickable: true,
        updateTriggers: { getColor: theme },
      }),
      new ArcLayer<ArcLeg>({
        id: "proposed-arcs",
        data: model.legs.arcs,
        getSourcePosition: (leg) => leg.from,
        getTargetPosition: (leg) => leg.to,
        getSourceColor: (leg) => rgba(linkColorRole(leg.link), theme),
        getTargetColor: (leg) => rgba(linkColorRole(leg.link), theme),
        getWidth: 3,
        widthUnits: "pixels",
        pickable: true,
        updateTriggers: { getSourceColor: theme, getTargetColor: theme },
      }),
      new ScatterplotLayer<MapNode>({
        id: "sites",
        data: model.nodes,
        getPosition: (n) => nodePosition(n),
        getRadius: (n) => nodeRadius(n),
        radiusUnits: "pixels",
        getFillColor: (n) => statusRgba(n.status, theme),
        getLineColor: surface,
        stroked: true,
        lineWidthUnits: "pixels",
        getLineWidth: 1.5,
        pickable: true,
        updateTriggers: { getFillColor: theme, getLineColor: theme },
      }),
    ];
  }, [model, theme]);

  const getTooltip = useMemo(() => {
    const style = {
      background: hexColor("surface", theme),
      color: hexColor("fg", theme),
      border: `1px solid ${hexColor("border", theme)}`,
      borderRadius: "8px",
      fontSize: "12px",
      padding: "6px 8px",
    };
    return ({ object }: { object?: Picked | null }) => {
      if (!object) return null;
      if (isNode(object)) {
        const units = object.units_at_risk ? ` · ${formatValue(object.units_at_risk, "units")} at risk` : "";
        return { text: `${object.label}\n${nodeKindLabels[object.kind]} · ${statusLabel(object.status)}${units}`, style };
      }
      if (isLeg(object)) {
        const link = object.link;
        const from = labels.get(link.from) ?? link.from;
        const to = labels.get(link.to) ?? link.to;
        const units = link.units !== null ? ` · ${formatValue(link.units, "units")}` : "";
        const option = link.option_id ? `\nProposed by ${optionTitles.get(link.option_id) ?? link.option_id}` : "";
        return { text: `${from} → ${to}\n${linkKindLabels[link.kind]} · ${statusLabel(link.status)}${units}${option}`, style };
      }
      return { text: `${object.properties.label}\n${statusLabel(object.properties.status)}`, style };
    };
  }, [theme, labels, optionTitles]);

  const overlayProps = useMemo<MapLibreOverlayProps>(
    () => ({ interleaved: false, layers, getTooltip: getTooltip as NonNullable<MapLibreOverlayProps["getTooltip"]> }),
    [layers, getTooltip],
  );

  return (
    <div
      className="overflow-hidden rounded-md border border-border"
      style={{ height }}
      data-map-style={styleFailed || kind === "plain" ? "fallback" : theme}
    >
      <MapView
        mapLib={maplibre}
        mapStyle={mapStyle}
        initialViewState={initialViewState}
        {...(maxZoom !== undefined ? { maxZoom } : {})}
        style={{ width: "100%", height: "100%" }}
        attributionControl={{ compact: false }}
        cooperativeGestures
        dragRotate={false}
        pitchWithRotate={false}
        touchPitch={false}
        fadeDuration={reducedMotion ? 0 : 300}
        onLoad={() => {
          loaded.current = true;
        }}
        onError={(event) => {
          // Before the map has loaded, an error means the base map itself is unavailable (style,
          // TileJSON / pmtiles archive, sprite, worker): keep the overlay on a plain background and
          // let NetworkMap try the next base map. A single tile that fails (`tile` on the event,
          // possibly before "load") or later errors are ignored.
          if (isBaseMapFailure(event, loaded.current) && !styleFailed) {
            setStyleFailed(true);
            onBaseMapError?.();
          }
        }}
      >
        <NavigationControl showCompass={false} position="top-right" />
        <DeckOverlay {...overlayProps} />
      </MapView>
    </div>
  );
}
