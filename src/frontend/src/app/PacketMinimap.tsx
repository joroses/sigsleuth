import { useEffect, useMemo, useRef } from "react";
import * as maplibregl from "maplibre-gl";

import { LinkQualityLegend } from "./MapLinkDetails";
import { nodeHref } from "./nodeDisplay";
import {
  TWO_WAY_OFFSET,
  type HopLines,
  type MinimapContents,
} from "./minimapMarkers";
import { NodeLink } from "./ui";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";

/**
 * A reception whose report the minimap shows, or a relay or a hop whose
 * reports it shows. A hop is only focused as the one contents.hopReports was found for.
 */
export type MinimapFocus =
  | { kind: "reception"; receptionId: number }
  | { kind: "relay"; nodeId: number }
  | { kind: "hop"; fromNodeId: number; toNodeId: number }
  | null;

const HOPS_SOURCE = "hops";
const HOP_SNR_LAYER = "hop-snr";
const ARROW_IMAGE = "hop-arrow";
// Each hop is one observation, so every line is drawn the same width.
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

// The sender and gateways of one packet, or a traceroute's ends, relays
// and hops, on the basemap chosen on the map page. Nodes sit where their own
// positions placed them when the packet was heard, else where they are now
// (minimapMarkers). With a reception in focus, its gateway's
// marker stands out and, for a traceroute, only the routes that gateway
// reported are drawn, with each relay's place on them. With a relay in focus,
// the reports through it are drawn, and with a hop in focus the reports that
// crossed it. Either way, relays off the drawn reports are hidden. Clicking a relay asks to focus it. On a traceroute, hovering or
// focusing S, R or a relay reports its node, and the highlighted node's marker
// stands out, so the route text beside the map can point at it. On other
// packets, hovering a gateway reports its reception.
export function PacketMinimap({
  contents: {
    markers,
    unplacedGateways,
    allHops,
    receptionHops,
    receptionNumbers,
    relayReports,
    hopReports,
    reports,
    labelAllHops = false,
  },
  focus,
  onClearFocus,
  onRelayClick,
  highlightedNode = null,
  onNodeHover,
  onReceptionHover,
  showLegend = true,
}: {
  contents: MinimapContents;
  focus: MinimapFocus;
  /** Offered as "Show all reports" while a hop is focused. */
  onClearFocus?: () => void;
  /** Relays are buttons calling this; without it they link to their node. */
  onRelayClick?: (nodeId: number) => void;
  highlightedNode?: number | null;
  /** Called with S, R or a relay's node when hovered or focused, then null. */
  onNodeHover?: (nodeId: number | null) => void;
  /** Called with a gateway's reception when hovered, then null. */
  onReceptionHover?: (receptionId: number | null) => void;
  showLegend?: boolean;
}) {
  const markerElements = useRef(new Map<string, HTMLElement>());
  // Kept in refs so a new handler does not rebuild the map.
  const relayClick = useRef(onRelayClick);
  const nodeHover = useRef(onNodeHover);
  const receptionHover = useRef(onReceptionHover);
  const focusedHops =
    focus?.kind === "reception"
      ? receptionHops.get(focus.receptionId)
      : undefined;
  // A hop no listed route crossed shows every route instead.
  const focusedReports =
    focus?.kind === "relay"
      ? relayReports.get(focus.nodeId)
      : focus?.kind === "hop" && hopReports?.receptionIds.size
        ? hopReports
        : undefined;
  const shown = focusedHops ?? focusedReports?.hops ?? allHops;
  const labelled = labelAllHops || shown !== allHops;
  const focusedMarkerKey =
    focus?.kind === "reception"
      ? `reception-${focus.receptionId}`
      : focus?.kind === "relay"
        ? `relay-${focus.nodeId}`
        : null;
  const numbers =
    focus?.kind === "reception"
      ? receptionNumbers.get(focus.receptionId)
      : undefined;

  const onRoute = useMemo(
    () =>
      numbers !== undefined ? new Set(numbers.keys()) : focusedReports?.nodeIds,
    [numbers, focusedReports],
  );

  useEffect(() => {
    relayClick.current = onRelayClick;
    nodeHover.current = onNodeHover;
    receptionHover.current = onReceptionHover;
  }, [onRelayClick, onNodeHover, onReceptionHover]);

  const { container, map, failure } = useBasemapMap(
    {
      enabled: markers.length > 0,
      // Arrow and label directions assume north is up and the map is flat.
      flat: true,
      view: () => ({
        bounds: markerBounds(markers),
        fitBoundsOptions: { padding: 40, maxZoom: 12 },
      }),
      onStyleLoad: (created, basemap) =>
        addHopLayers(created, basemap.darkLabels === false),
      setup: (created) => {
        // Later markers are drawn on top, so add them from the last. The sender is
        // a pin standing above its position, so a marker at the same spot stays
        // visible under it. Relays are buttons showing their routes; the other
        // markers link to their node.
        const elements = markerElements.current;
        for (const marker of [...markers].reverse()) {
          const isSender = marker.kind === "sender";
          let element: HTMLElement;
          if (marker.kind === "relay" && relayClick.current !== undefined) {
            element = document.createElement("button");
            element.setAttribute("type", "button");
            element.addEventListener("click", () =>
              relayClick.current?.(marker.nodeId),
            );
          } else {
            const link = document.createElement("a");
            link.href = nodeHref(marker.nodeId);
            element = link;
          }
          element.className = `minimap-marker minimap-${marker.kind} ${marker.tone}${
            marker.estimated ? " estimated" : ""
          }`;
          element.title = marker.title;
          element.textContent = marker.label;
          if (marker.kind === "gateway") {
            const receptionId = Number(marker.key.slice("reception-".length));
            element.addEventListener("mouseenter", () =>
              receptionHover.current?.(receptionId),
            );
            element.addEventListener("mouseleave", () =>
              receptionHover.current?.(null),
            );
          } else {
            const enter = () => nodeHover.current?.(marker.nodeId);
            const leave = () => nodeHover.current?.(null);
            element.addEventListener("mouseenter", enter);
            element.addEventListener("focus", enter);
            element.addEventListener("mouseleave", leave);
            element.addEventListener("blur", leave);
          }
          elements.set(marker.key, element);
          new maplibregl.Marker(
            // The pin's point is 6 px below its box (see .minimap-sender::after).
            isSender
              ? { element, anchor: "bottom", offset: [0, -6] }
              : { element },
          )
            .setLngLat([marker.longitude, marker.latitude])
            .addTo(created);
        }
        return () => elements.clear();
      },
    },
    [markers],
  );

  useEffect(() => {
    if (map === null) return;
    map
      .getSource<maplibregl.GeoJSONSource>(HOPS_SOURCE)
      ?.setData(shown.features);
    // SNR labels only on a focused route: on all hops they crowd the lines.
    map.setLayoutProperty(
      HOP_SNR_LAYER,
      "visibility",
      labelled ? "visible" : "none",
    );
  }, [map, shown, labelled]);

  useEffect(() => {
    const element =
      focusedMarkerKey === null
        ? undefined
        : markerElements.current.get(focusedMarkerKey);
    element?.classList.add("focused");
    return () => element?.classList.remove("focused");
  }, [map, focusedMarkerKey]);

  useEffect(() => {
    if (highlightedNode === null) return;
    const highlighted: HTMLElement[] = [];
    for (const marker of markers) {
      if (marker.kind === "gateway" || marker.nodeId !== highlightedNode) {
        continue;
      }
      const element = markerElements.current.get(marker.key);
      if (element === undefined) continue;
      element.classList.add("highlighted");
      highlighted.push(element);
    }
    return () => {
      for (const element of highlighted) {
        element.classList.remove("highlighted");
      }
    };
  }, [map, markers, highlightedNode]);

  useEffect(() => {
    if (numbers === undefined) return;
    const numbered: HTMLElement[] = [];
    for (const [nodeId, label] of numbers) {
      const element = markerElements.current.get(`relay-${nodeId}`);
      if (element === undefined) continue;
      element.textContent = label;
      numbered.push(element);
    }
    return () => {
      for (const element of numbered) element.textContent = "";
    };
  }, [map, numbers]);

  useEffect(() => {
    if (onRoute === undefined) return;
    const hidden: HTMLElement[] = [];
    for (const marker of markers) {
      if (marker.kind !== "relay" || onRoute.has(marker.nodeId)) continue;
      const element = markerElements.current.get(marker.key);
      if (element === undefined) continue;
      element.classList.add("off-route");
      hidden.push(element);
    }
    return () => {
      for (const element of hidden) element.classList.remove("off-route");
    };
  }, [map, markers, onRoute]);

  const relayMarker =
    focus?.kind === "relay"
      ? markers.find((marker) => marker.key === focusedMarkerKey)
      : undefined;
  // S, R or a relay; a node with no marker shows its ID.
  const markedNode = (nodeId: number) => (
    <NodeLink
      nodeId={nodeId}
      name={markers.find((marker) => marker.nodeId === nodeId)?.name ?? null}
    />
  );
  return (
    <figure className="packet-minimap">
      {failure !== null ? (
        <p className="muted">{MAP_FAILURE_MESSAGES[failure]}</p>
      ) : null}
      <div
        ref={container}
        className="minimap-canvas"
        hidden={failure !== null}
      />
      <figcaption className="muted">
        Positions when heard, else latest.
        {unplacedGateways > 0
          ? ` ${unplacedGateways} ${unplacedGateways === 1 ? "gateway has" : "gateways have"} no known position.`
          : null}
        {focusedHops !== undefined
          ? focusedHops.hops === 0
            ? " This gateway's report has no hops."
            : " Showing what this gateway reported."
          : null}
        {focusedReports !== undefined && relayMarker !== undefined ? (
          <>
            {" "}
            Reports through{" "}
            <NodeLink
              nodeId={relayMarker.nodeId}
              name={relayMarker.name}
            />: {focusedReports.receptionIds.size} of {reports} reports.
          </>
        ) : null}
        {focus?.kind === "hop" ? (
          focusedReports !== undefined ? (
            <>
              {" "}
              Reports with the hop {markedNode(focus.fromNodeId)} →{" "}
              {markedNode(focus.toNodeId)}: {focusedReports.receptionIds.size}{" "}
              of {reports} reports.{" "}
              {onClearFocus !== undefined ? (
                <button
                  type="button"
                  className="text-button"
                  onClick={onClearFocus}
                >
                  Show all reports
                </button>
              ) : null}
            </>
          ) : (
            <>
              {" "}
              No listed report has the hop {markedNode(focus.fromNodeId)} →{" "}
              {markedNode(focus.toNodeId)}, so all reports are shown.
            </>
          )
        ) : null}
        {undrawnHopsNote(shown)}
      </figcaption>
      {showLegend && allHops.hops > 0 ? <LinkQualityLegend /> : null}
    </figure>
  );
}

function markerBounds(markers: MinimapContents["markers"]) {
  const bounds = new maplibregl.LngLatBounds();
  for (const marker of markers) {
    bounds.extend([marker.longitude, marker.latitude]);
  }
  return bounds;
}

function undrawnHopsNote({ hops, undrawnHops }: HopLines): string | null {
  return undrawnHops > 0
    ? ` ${undrawnHops} of ${hops} route hops not drawn: a node has no known position.`
    : null;
}

// Hop lines coloured by SNR as on the map (dashed without one), an arrow at
// each line's middle pointing the way the packet went, and SNR labels beside
// the arrows. A pair crossed both ways is drawn as two lines, each offset to
// its own right like a two-way road.
function addHopLayers(map: maplibregl.Map, darkBasemap: boolean): void {
  map.addImage(ARROW_IMAGE, arrowImage(), { sdf: true, pixelRatio: 2 });
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
  map.addLayer({
    id: "hop-lines",
    type: "line",
    source: HOPS_SOURCE,
    filter: ["all", isLine, hasSnr],
    paint: linePaint,
  });
  map.addLayer({
    id: "hop-lines-no-snr",
    type: "line",
    source: HOPS_SOURCE,
    filter: ["all", isLine, ["!", hasSnr]],
    paint: { ...linePaint, "line-dasharray": [2, 2] },
  });
  map.addLayer({
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
      "icon-color": ["get", "color"],
      "icon-halo-color": halo,
      "icon-halo-width": 1,
    },
  });
  map.addLayer({
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
  });
}

// A triangle pointing east as a signed distance field, so MapLibre can tint it
// with each line's colour: alpha 191 is the edge, rising 32 a pixel inwards.
const ARROW_SIZE = 28;
const ARROW_CORNERS: [number, number][] = [
  [5, 5],
  [24, 14],
  [5, 23],
];

function arrowImage(): {
  width: number;
  height: number;
  data: Uint8Array;
} {
  const data = new Uint8Array(ARROW_SIZE * ARROW_SIZE * 4);
  for (let y = 0; y < ARROW_SIZE; y += 1) {
    for (let x = 0; x < ARROW_SIZE; x += 1) {
      // Distance inside the triangle: the least distance to an edge, negative outside.
      let inside = Infinity;
      for (let index = 0; index < 3; index += 1) {
        const [ax, ay] = ARROW_CORNERS[index];
        const [bx, by] = ARROW_CORNERS[(index + 1) % 3];
        const length = Math.hypot(bx - ax, by - ay);
        const distance =
          ((bx - ax) * (y + 0.5 - ay) - (by - ay) * (x + 0.5 - ax)) / length;
        inside = Math.min(inside, distance);
      }
      const alpha = Math.max(0, Math.min(255, 191 + inside * 32));
      data[(y * ARROW_SIZE + x) * 4 + 3] = alpha;
    }
  }
  return { width: ARROW_SIZE, height: ARROW_SIZE, data };
}
