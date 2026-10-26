import { useEffect, useMemo, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";

import { type PacketRelays, relayPositions } from "./packetRelays";
import { LinkQualityLegend } from "./MapLinkDetails";
import {
  ARROW_HALO,
  ARROW_IMAGE,
  addArrowImage,
  zeroCostColor,
} from "./mapLinkLayers";
import { nodeHref } from "./nodeDisplay";
import { useLinkedSetting } from "./remember";
import type { PacketLinkView } from "./routes";
import {
  TWO_WAY_OFFSET,
  type DirectedHop,
  type HopLines,
  type MinimapContents,
} from "./minimapMarkers";
import { Icon, IconButton, NodeLink, ShareButton } from "./ui";
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
const HOP_HIT_LAYER = "hop-hit";
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

const FLOOD_SOURCE = "flood";
const FLOOD_ROUTE_LINES = "flood-routes";
const FLOOD_ROUTE_GUESS_LINES = "flood-routes-guess";
const ROUTE_LINE_WIDTH = 1.5;
const FLOOD_ARROWS = "flood-arrows";
const FLOOD_RELAYS = "flood-relays";
const FLOOD_RELAY_NAMES = "flood-relay-names";
const RELAY_DOT_COLOR = "#64748b";
// With its stroke, 22 px across: as big as a gateway marker (.minimap-marker).
const RELAY_DOT_RADIUS = 9;
const RELAY_DOT_STROKE = 2;

// Packet reports and traceroute hops on the map page's chosen basemap.
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
  highlightedHop = null,
  onNodeHover,
  onHopHover,
  onHopClick,
  onReceptionHover,
  showLegend = true,
  relayMap = null,
  relaysOnOpen = false,
  onRelaysShow,
  expandable = false,
  linkView = null,
  share,
}: {
  contents: MinimapContents;
  focus: MinimapFocus;
  /** Offered as "Show all reports" while a hop is focused. */
  onClearFocus?: () => void;
  /** Relays are buttons calling this; without it they link to their node. */
  onRelayClick?: (nodeId: number) => void;
  highlightedNode?: number | null;
  highlightedHop?: DirectedHop | null;
  onHopHover?: (hop: DirectedHop | null) => void;
  onHopClick?: (hop: DirectedHop) => void;
  /** Called with S, R or a relay's node when hovered or focused, then null. */
  onNodeHover?: (nodeId: number | null) => void;
  /** Called with a gateway's reception when hovered, then null. */
  onReceptionHover?: (receptionId: number | null) => void;
  showLegend?: boolean;
  /** Offers the packet's relays and their connections. */
  relayMap?: PacketRelays | null;
  /** Show relays once the map is ready. */
  relaysOnOpen?: boolean;
  /** Called when relays are shown. */
  onRelaysShow?: () => void;
  /** Offers a bigger map over the page. */
  expandable?: boolean;
  /** The switches and size a shared link set, over the saved switches. */
  linkView?: PacketLinkView | null;
  /** Offers a link to the map as shown, built by `href`. */
  share?: { title: string; href: (view: PacketLinkView) => string };
}) {
  const markerElements = useRef(new Map<string, HTMLElement>());
  // Kept in refs so a new handler does not rebuild the map.
  const relayClick = useRef(onRelayClick);
  const nodeHover = useRef(onNodeHover);
  const receptionHover = useRef(onReceptionHover);
  const hopHover = useRef(onHopHover);
  const hopClick = useRef(onHopClick);
  const interactiveHops = onHopHover !== undefined;
  const [relaysEnabled, setRelaysEnabled] = useLinkedSetting(
    "packet.map.relays",
    false,
    relaysOnOpen ? true : linkView?.show.includes("relays"),
  );
  const [gatewaysEnabled, setGatewaysEnabled] = useLinkedSetting(
    "packet.map.gateways",
    !relaysOnOpen,
    linkView?.show.includes("gateways"),
  );
  // With nothing else placed, the relays are the map.
  const onlyRelays = markers.length === 0;
  const relaysShown = relayMap !== null && (relaysEnabled || onlyRelays);
  const gatewaysShown = relayMap === null || gatewaysEnabled;
  useEffect(() => {
    if (relaysShown) onRelaysShow?.();
  }, [relaysShown, onRelaysShow]);
  const [expanded, setExpanded] = useState(linkView?.expanded ?? false);
  useEffect(() => {
    if (!expanded) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [expanded]);
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
    hopHover.current = onHopHover;
    hopClick.current = onHopClick;
  }, [onRelayClick, onNodeHover, onReceptionHover, onHopHover, onHopClick]);

  const { container, map, failure } = useBasemapMap(
    {
      enabled: markers.length > 0 || relayPositions(relayMap).length > 0,
      // Arrow and label directions assume north is up and the map is flat.
      flat: true,
      view: () => ({
        bounds: onlyRelays
          ? pointBounds(relayPositions(relayMap))
          : markerBounds(markers),
        fitBoundsOptions: { padding: 40, maxZoom: 12 },
      }),
      onStyleLoad: (created, basemap) => {
        addHopLayers(created, basemap.darkLabels === false);
        addFloodLayers(created, basemap.darkLabels === false);
      },
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
            element.addEventListener("click", (event) => {
              event.stopPropagation();
              relayClick.current?.(marker.nodeId);
            });
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
    if (map === null || !interactiveHops) return;
    const layers = [HOP_HIT_LAYER, "hop-arrows", HOP_SNR_LAYER];
    let hovered: DirectedHop | null = null;
    const hopAt = (
      event: maplibregl.MapLayerMouseEvent,
    ): DirectedHop | null => {
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
    };
    const move = (event: maplibregl.MapLayerMouseEvent) => {
      const hop = hopAt(event);
      if (
        hop?.fromNodeId === hovered?.fromNodeId &&
        hop?.toNodeId === hovered?.toNodeId
      )
        return;
      hovered = hop;
      map.getCanvas().style.cursor = hop === null ? "" : "pointer";
      hopHover.current?.(hop);
    };
    const leave = () => {
      hovered = null;
      map.getCanvas().style.cursor = "";
      hopHover.current?.(null);
    };
    const click = (event: maplibregl.MapLayerMouseEvent) => {
      const hop = hopAt(event);
      if (hop !== null) hopClick.current?.(hop);
    };
    map.on("mousemove", layers, move);
    map.on("mouseleave", layers, leave);
    map.on("click", layers, click);
    return () => {
      map.off("mousemove", layers, move);
      map.off("mouseleave", layers, leave);
      map.off("click", layers, click);
      leave();
    };
  }, [map, interactiveHops]);

  useEffect(() => {
    if (map === null) return;
    const width: maplibregl.ExpressionSpecification | number =
      highlightedHop === null
        ? HOP_LINE_WIDTH
        : [
            "case",
            [
              "all",
              ["==", ["get", "fromNodeId"], highlightedHop.fromNodeId],
              ["==", ["get", "toNodeId"], highlightedHop.toNodeId],
            ],
            5,
            HOP_LINE_WIDTH,
          ];
    for (const layer of ["hop-lines", "hop-lines-no-snr"]) {
      map.setPaintProperty(layer, "line-width", width);
    }
  }, [map, highlightedHop]);

  useEffect(() => {
    if (map === null) return;
    map.getSource<maplibregl.GeoJSONSource>(FLOOD_SOURCE)?.setData({
      type: "FeatureCollection",
      features: relaysShown && relayMap !== null ? relayMap.features : [],
    });
  }, [map, relayMap, relaysShown]);

  useEffect(() => {
    if (map === null) return;
    for (const marker of markers) {
      if (marker.kind !== "gateway") continue;
      const element = markerElements.current.get(marker.key);
      element?.classList.toggle("off-route", !gatewaysShown);
    }
  }, [map, markers, gatewaysShown]);

  useEffect(() => {
    if (map === null) return;
    const bounds = markerBounds(
      markers.filter((marker) => marker.kind !== "gateway" || gatewaysShown),
    );
    if (relaysShown) {
      for (const position of relayPositions(relayMap)) bounds.extend(position);
    }
    if (!bounds.isEmpty()) map.fitBounds(bounds, { padding: 40, maxZoom: 12 });
  }, [map, markers, relayMap, relaysShown, gatewaysShown]);

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
    <>
      {expanded ? (
        <div
          className="node-position-backdrop"
          onClick={() => setExpanded(false)}
        />
      ) : null}
      <figure
        className={`packet-minimap node-position ${expanded ? "node-position-expanded" : ""}`}
      >
        {relayMap !== null || expandable || share !== undefined ? (
          <div className="node-position-head">
            {relayMap !== null ? (
              <div
                className="minimap-toggles"
                role="group"
                aria-label="Show on the map"
              >
                <button
                  type="button"
                  aria-pressed={relaysShown}
                  disabled={onlyRelays}
                  onClick={() => setRelaysEnabled(!relaysEnabled)}
                >
                  <Icon name="route" />
                  Relays
                </button>
                <button
                  type="button"
                  aria-pressed={gatewaysShown}
                  onClick={() => setGatewaysEnabled(!gatewaysEnabled)}
                >
                  <Icon name="radio" />
                  Gateways
                </button>
              </div>
            ) : null}
            {expandable || share !== undefined ? (
              <div className="minimap-actions">
                {expandable ? (
                  <IconButton
                    icon={expanded ? "close" : "maximize"}
                    label={
                      expanded ? "Close the bigger map (Esc)" : "Expand the map"
                    }
                    aria-pressed={expanded}
                    onClick={() => setExpanded(!expanded)}
                  />
                ) : null}
                {share !== undefined ? (
                  <ShareButton
                    hash={share.href({
                      expanded,
                      show:
                        relayMap === null
                          ? []
                          : [
                              ...(relaysShown ? (["relays"] as const) : []),
                              ...(gatewaysShown ? (["gateways"] as const) : []),
                            ],
                    })}
                    title={share.title}
                  />
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="node-position-map">
          {failure !== null ? (
            <p className="muted">{MAP_FAILURE_MESSAGES[failure]}</p>
          ) : null}
          <div
            ref={container}
            className="minimap-canvas"
            hidden={failure !== null}
          />
        </div>
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
                No listed report has the hop {markedNode(
                  focus.fromNodeId,
                )} → {markedNode(focus.toNodeId)}, so all reports are shown.
              </>
            )
          ) : null}
          {undrawnHopsNote(shown)}
          {showLegend && relaysShown ? (
            <>
              <br />
              Relays shown are likely to have forwarded this packet. Lines show
              likely routes between them. Zero-hop forwarding can make these
              estimates less reliable.
            </>
          ) : null}
        </figcaption>
        {showLegend && allHops.hops > 0 ? (
          <LinkQualityLegend
            zeroCost={allHops.features.features.some(
              (feature) => feature.properties.zeroCost,
            )}
          />
        ) : null}
      </figure>
    </>
  );
}

function markerBounds(markers: MinimapContents["markers"]) {
  return pointBounds(
    markers.map((marker): [number, number] => [
      marker.longitude,
      marker.latitude,
    ]),
  );
}

function pointBounds(points: [number, number][]) {
  const bounds = new maplibregl.LngLatBounds();
  for (const point of points) bounds.extend(point);
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
  map.addLayer({
    id: HOP_HIT_LAYER,
    type: "line",
    source: HOPS_SOURCE,
    filter: isLine,
    paint: { ...linePaint, "line-width": 10, "line-opacity": 0 },
  });
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
      "icon-color": [
        "case",
        ["get", "zeroCost"],
        zeroCostColor(),
        ["get", "color"],
      ],
      "icon-halo-color": ["case", ["get", "zeroCost"], ARROW_HALO, halo],
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

// Relay connections are dashed when guessed; hollow dots mark estimated positions.
function addFloodLayers(map: maplibregl.Map, darkBasemap: boolean): void {
  map.addSource(FLOOD_SOURCE, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  const halo = darkBasemap ? "#0f172a" : "#ffffff";
  const routePaint = {
    "line-color": ["get", "color"],
    "line-width": ROUTE_LINE_WIDTH,
  } satisfies maplibregl.LineLayerSpecification["paint"];
  map.addLayer({
    id: FLOOD_ROUTE_LINES,
    filter: [
      "all",
      ["==", ["get", "kind"], "route"],
      ["==", ["get", "certainty"], "known"],
    ],
    type: "line",
    source: FLOOD_SOURCE,
    paint: routePaint,
  });
  map.addLayer({
    id: FLOOD_ROUTE_GUESS_LINES,
    filter: [
      "all",
      ["==", ["get", "kind"], "route"],
      ["==", ["get", "certainty"], "guess"],
    ],
    type: "line",
    source: FLOOD_SOURCE,
    paint: { ...routePaint, "line-dasharray": [2, 2] },
  });
  map.addLayer({
    id: FLOOD_ARROWS,
    filter: ["==", ["get", "kind"], "route-arrow"],
    type: "symbol",
    source: FLOOD_SOURCE,
    layout: {
      "icon-image": ARROW_IMAGE,
      "icon-rotate": ["get", "rotation"],
      "icon-rotation-alignment": "map",
      "icon-size": 0.7,
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
    id: FLOOD_RELAYS,
    filter: ["==", ["get", "kind"], "relay"],
    type: "circle",
    source: FLOOD_SOURCE,
    paint: {
      "circle-radius": RELAY_DOT_RADIUS,
      "circle-color": ["case", ["get", "estimated"], halo, RELAY_DOT_COLOR],
      "circle-stroke-color": [
        "case",
        ["get", "estimated"],
        RELAY_DOT_COLOR,
        halo,
      ],
      "circle-stroke-width": RELAY_DOT_STROKE,
    },
  });
  map.addLayer({
    id: FLOOD_RELAY_NAMES,
    filter: ["==", ["get", "kind"], "relay"],
    type: "symbol",
    source: FLOOD_SOURCE,
    layout: {
      "text-field": ["coalesce", ["get", "label"], ""],
      "text-font": ["Noto Sans Bold"],
      "text-size": 11,
      "text-anchor": "left",
      // In ems of the 11 px text: clear of the dot's edge by 3 px.
      "text-offset": [(RELAY_DOT_RADIUS + RELAY_DOT_STROKE + 3) / 11, 0],
      "text-optional": true,
    },
    paint: {
      "text-color": darkBasemap ? "#f8fafc" : "#1e293b",
      "text-halo-color": halo,
      "text-halo-width": 1.5,
    },
  });
}
