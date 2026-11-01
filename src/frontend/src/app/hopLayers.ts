// The traceroute hop layers and route markers shared by the packet minimap
// and the main map.
import * as maplibregl from "maplibre-gl";

import {
  ARROW_HALO,
  ARROW_IMAGE,
  addArrowImage,
  zeroCostColor,
} from "./mapLinkLayers";
import {
  TWO_WAY_OFFSET,
  type DirectedHop,
  type MinimapMarker,
} from "./minimapMarkers";

export const HOPS_SOURCE = "hops";
export const HOP_SNR_LAYER = "hop-snr";
const HOP_HIT_LAYER = "hop-hit";
/** The layers where the pointer finds a hop (see hopAt). */
export const HOP_POINTER_LAYERS = [HOP_HIT_LAYER, "hop-arrows", HOP_SNR_LAYER];
// Each hop is one observation, so unhighlighted lines have the same width.
const HOP_LINE_WIDTH = 2.5;
const HOP_LINE_OPACITY = 0.8;
const isLine: maplibregl.ExpressionSpecification = [
  "==",
  ["geometry-type"],
  "LineString",
];
const isMark: maplibregl.ExpressionSpecification = [
  "==",
  ["geometry-type"],
  "Point",
];
const hasSnr: maplibregl.ExpressionSpecification = ["!=", ["get", "snr"], null];

// Hop lines coloured by SNR as on the map (dashed without one), an arrow at
// each line's middle pointing the way the packet went, and SNR labels beside
// the arrows. A pair crossed both ways is drawn as two lines, each offset to
// its own right like a two-way road. The layers go under beforeId when given.
export function addHopLayers(
  map: maplibregl.Map,
  darkBasemap: boolean,
  beforeId?: string,
): void {
  addArrowImage(map);
  map.addSource(HOPS_SOURCE, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  const halo = darkBasemap ? "#0f172a" : "#ffffff";
  const linePaint = {
    "line-color": ["get", "color"],
    "line-width": HOP_LINE_WIDTH,
    "line-opacity": HOP_LINE_OPACITY,
    "line-offset": ["case", ["get", "twoWay"], TWO_WAY_OFFSET, 0],
  } satisfies maplibregl.LineLayerSpecification["paint"];
  const layers: maplibregl.AddLayerObject[] = [
    {
      id: HOP_HIT_LAYER,
      type: "line",
      source: HOPS_SOURCE,
      filter: isLine,
      paint: { ...linePaint, "line-width": 10, "line-opacity": 0 },
    },
    {
      id: "hop-lines",
      type: "line",
      source: HOPS_SOURCE,
      filter: ["all", isLine, hasSnr],
      paint: linePaint,
    },
    {
      id: "hop-lines-no-snr",
      type: "line",
      source: HOPS_SOURCE,
      filter: ["all", isLine, ["!", hasSnr]],
      paint: { ...linePaint, "line-dasharray": [2, 2] },
    },
    {
      id: "hop-arrows",
      type: "symbol",
      source: HOPS_SOURCE,
      filter: isMark,
      layout: {
        "icon-image": ARROW_IMAGE,
        "icon-rotate": ["get", "rotation"],
        "icon-rotation-alignment": "map",
        "icon-offset": ["get", "arrowOffset"],
        "icon-allow-overlap": true,
        "icon-ignore-placement": true,
      },
      paint: {
        "icon-color": [
          "case",
          ["get", "zeroCost"],
          zeroCostColor(),
          ["get", "color"],
        ],
        "icon-halo-color": ["case", ["get", "zeroCost"], ARROW_HALO, halo],
        "icon-halo-width": 1,
      },
    },
    {
      id: HOP_SNR_LAYER,
      type: "symbol",
      source: HOPS_SOURCE,
      filter: ["all", isMark, hasSnr],
      layout: {
        visibility: "none",
        "text-field": ["get", "label"],
        "text-font": ["Noto Sans Bold"],
        "text-size": 11,
        "text-anchor": ["get", "labelAnchor"],
        "text-offset": ["get", "labelOffset"],
      },
      paint: {
        "text-color": darkBasemap ? "#f8fafc" : "#1e293b",
        "text-halo-color": halo,
        "text-halo-width": 1.5,
      },
    },
  ];
  for (const layer of layers) map.addLayer(layer, beforeId);
}

/** Draws one hop of addHopLayers' lines wider; null draws none wider. */
export function widenHop(map: maplibregl.Map, hop: DirectedHop | null): void {
  const width: maplibregl.ExpressionSpecification | number =
    hop === null
      ? HOP_LINE_WIDTH
      : [
          "case",
          [
            "all",
            ["==", ["get", "fromNodeId"], hop.fromNodeId],
            ["==", ["get", "toNodeId"], hop.toNodeId],
          ],
          5,
          HOP_LINE_WIDTH,
        ];
  for (const layer of ["hop-lines", "hop-lines-no-snr"]) {
    map.setPaintProperty(layer, "line-width", width);
  }
}

/** The hop under the pointer on HOP_POINTER_LAYERS; none under a marker. */
export function hopAt(
  event: maplibregl.MapLayerMouseEvent,
): DirectedHop | null {
  if (
    event.originalEvent.target instanceof Element &&
    event.originalEvent.target.closest(".minimap-marker")
  )
    return null;
  const properties = event.features?.[0]?.properties;
  return typeof properties?.fromNodeId === "number" &&
    typeof properties.toNodeId === "number"
    ? { fromNodeId: properties.fromNodeId, toNodeId: properties.toNodeId }
    : null;
}

/** A marker drawn as element, labelled and placed as `marker` says. */
export function routeMarker(
  marker: MinimapMarker,
  element: HTMLElement,
): maplibregl.Marker {
  element.className = `minimap-marker minimap-${marker.kind} ${marker.tone}${
    marker.estimated ? " estimated" : ""
  }`;
  element.title = marker.title;
  element.textContent = marker.label;
  return new maplibregl.Marker(
    // The pin's point is 6 px below its box (see .minimap-sender::after).
    marker.kind === "sender"
      ? { element, anchor: "bottom", offset: [0, -6] }
      : { element },
  ).setLngLat([marker.longitude, marker.latitude]);
}
