import { useEffect, useMemo, useRef } from "react";
import * as maplibregl from "maplibre-gl";

import type { NodeSummary, NetworkZones } from "./api";
import {
  RELAY_COLOR,
  RELAY_MAST,
  RELAY_MAST_TIP,
  WARNING_COLOR,
  drawnLinks,
  estimatedNodes,
  formatShare,
  mostSolidLink,
  pairKey,
  placed,
  relayConcern,
  relayMark,
  relayReason,
  solidPathLinks,
  textReachLookup,
  hullCentre,
  zoneHull,
} from "./zones";
import { nearestLine } from "./lineHit";
import { linkTierColor, marginTier } from "./linkQuality";
import {
  addLinkLayers,
  linkHalves,
  linkSourceId,
  noSnrLayerId,
  showLinkFocus,
} from "./mapLinkLayers";
import {
  SHARE_COLORS,
  badgeImage,
  imageCanvas,
  tierOfShare,
} from "./mapNodeLayers";
import { formatNodeId, nodeName } from "./nodeDisplay";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";

// Zone names come from GeoNames' cities (scripts/cities.py).
const PLACES_ATTRIBUTION =
  'Places: <a href="https://www.geonames.org/">GeoNames</a> (CC BY 4.0)';

// The Network network page's map: each zone is a shaded area with a tag
// bearing its number in the middle of the area (hullCentre), its name and
// member count beside the tag where they fit; the tags always show. A zone too
// small for an area has its tag at its centre (Zone.latitude and longitude).
// One line joins each pair of zones that share heard links, drawn between the
// two nodes of its most solid link (mostSolidLink), where the zones actually
// join, each end marked with a dot in its zone's colour; it joins the zones'
// tags only when no link has both nodes placed.
// A line's colour is its strongest link's weaker margin. It is wide with 2 or
// more solid links (solid links between the two zones with no node in
// common), thin with 1, and dashed with none. Key relays whose going quiet
// leaves 2 or more nodes with only weak or one-way links, or none, are bare
// red radio masts; those that carry the only solid link between two zones (a
// thin line's) are white
// masts on a red disc, drawn above. A key relay not heard for a while or on a
// low battery (relayConcern) has an amber warning triangle at its mast's
// top right, or, with no mast, the triangle alone. Clicking a mast or a
// triangle selects its relay, ringed in orange, and ZonesView opens its
// details beside the map.
// A node with no reported position is drawn at its estimate as a hollow dot,
// as on the Map page; its hover name says so. It is left out of its zone's
// shaded area.
// Selecting a pair draws its links as the Map page does, in two halves coloured
// by what each end hears, and enlarges the nodes that carry them; clicking one
// of those links opens its details beside the map (ZonesView). Hovering a node
// marker names the node. Selecting a zone badges every other zone's circle
// with how often the selected zone's texts reached it, and draws its shortest
// solid paths to the other zones link by link, as a pair's links are drawn,
// in place of the lines between zones.

const ZONES = "zones-zones";
const ZONE_LINES = "zones-zone-lines";
const LINE_ENDS = "zones-line-ends";
const RELAYS = "zones-relays";
const WARNING_ICON = "zones-relay-warning";
const MEMBERS = "zones-members";
// The selected pair's links use the Map page's traceroute link layers.
const PAIR_LINKS = linkSourceId("traceroute");
const PAIR_LINK_LAYERS = [PAIR_LINKS, noSnrLayerId("traceroute")];
const PAIR_ENDS = "zones-pair-ends";
const REACH_BADGES = "zones-reach-badges";
// The badge of a zone no gateway in which reported a text.
const NO_GATEWAY_COLOR = "#cbd5e1";
// A zone tag's size in pixels: at least as wide as a two-digit number's.
const TAG_HEIGHT = 18;
const TAG_MIN_WIDTH = 24;

type RouteStyle = "solid" | "dashed";
const ROUTE_DASHES: Record<RouteStyle, number[] | null> = {
  solid: null,
  dashed: [2, 1.5],
};
// Line widths in pixels: 2 or more solid links, one, or none.
const WIDE_LINE = 7;
const THIN_LINE = 2.5;
const LINE_LAYERS = (Object.keys(ROUTE_DASHES) as RouteStyle[]).map(
  (style) => `${ZONE_LINES}-${style}`,
);
const SELECTED_RELAY = "zones-selected-relay";
// A lone warning triangle's width in pixels; over a mast it grows with the
// mast (warningWidth).
const LONE_WARNING_WIDTH = 16;
// The ring around the selected relay, as the Map page's selected node.
const SELECTED_COLOR = "#f5a623";
// Node markers that name their node when hovered, top-most first.
const NODE_LAYERS = [
  `${RELAYS}-warnings`,
  `${RELAYS}-markers`,
  `${PAIR_ENDS}-dots`,
  `${LINE_ENDS}-dots`,
  `${MEMBERS}-dots`,
];
const EMPTY: GeoJSON.FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

/**
 * Where a row beside the map asked the map to go: the points to fit, as
 * [longitude, latitude]. A new object each time, so a second click on the
 * same row goes there again.
 */
export interface MapFocus {
  points: [number, number][];
}

export function ZoneMap({
  network,
  nodes,
  colors,
  names,
  selectedZone,
  onSelectZone,
  selectedPair,
  onSelectPair,
  openLink,
  onOpenLink,
  selectedRelay,
  onSelectRelay,
  onBack,
  focus,
}: {
  network: NetworkZones;
  nodes: Map<number, NodeSummary>;
  colors: Map<number, string>;
  names: Map<number, string>;
  selectedZone: number | null;
  onSelectZone: (zoneId: number) => void;
  selectedPair: string | null;
  onSelectPair: (pair: string) => void;
  /** The selected pair's open link, as `${nodeA}-${nodeB}`. */
  openLink: string | null;
  onOpenLink: (link: string) => void;
  selectedRelay: number | null;
  onSelectRelay: (nodeId: number) => void;
  /** A click on empty map goes back one step, like the side panel's back. */
  onBack: () => void;
  focus: MapFocus | null;
}) {
  // Kept in refs so new handlers do not rebuild the map.
  const selectZone = useRef(onSelectZone);
  const selectPair = useRef(onSelectPair);
  const openPairLink = useRef(onOpenLink);
  const selectRelay = useRef(onSelectRelay);
  const back = useRef(onBack);
  useEffect(() => {
    selectZone.current = onSelectZone;
    selectPair.current = onSelectPair;
    openPairLink.current = onOpenLink;
    selectRelay.current = onSelectRelay;
    back.current = onBack;
  }, [onSelectZone, onSelectPair, onOpenLink, onSelectRelay, onBack]);

  const data = useMemo(
    () => mapFeatures(network, nodes, colors, names),
    [network, nodes, colors, names],
  );
  const estimated = useMemo(() => estimatedNodes(network), [network]);
  const zoneOf = useMemo(
    () =>
      new Map(
        network.zones.flatMap((zone) =>
          zone.members.map((id) => [id, zone.zone_id] as const),
        ),
      ),
    [network],
  );
  const pair = network.zone_pairs.find(
    (item) => pairKey(item) === selectedPair,
  );
  // The links drawn node to node: the selected pair's, else the selected
  // zone's solid paths.
  const nodeLinks = useMemo(
    () =>
      drawnLinks(pair?.links ?? solidPathLinks(network, selectedZone), nodes),
    [network, nodes, pair, selectedZone],
  );

  const { container, map, failure } = useBasemapMap(
    {
      flat: true,
      attribution: PLACES_ATTRIBUTION,
      view: () => ({ center: [0, 20], zoom: 1 }),
      onStyleLoad: (created) => {
        addLayers(created);
        // What a click at the point takes: key relays first, then a selected
        // pair's links, zone tags, the lines between zones and the zone areas.
        // Lines are taken from a few pixels away (nearestLine), the nearest
        // first.
        const at = (point: maplibregl.Point) => {
          const exact = (layers: string[]) =>
            created.queryRenderedFeatures(point, { layers })[0];
          return (
            exact([`${RELAYS}-warnings`, `${RELAYS}-markers`]) ??
            nearestLine(created, point, PAIR_LINK_LAYERS) ??
            exact([`${ZONES}-label`]) ??
            nearestLine(created, point, LINE_LAYERS) ??
            exact([`${ZONES}-fill`])
          );
        };
        created.on("click", (event) => {
          const properties = at(event.point)?.properties ?? {};
          if (typeof properties.relay === "number") {
            selectRelay.current(properties.relay);
            return;
          }
          if (typeof properties.nodeA === "number") {
            openPairLink.current(`${properties.nodeA}-${properties.nodeB}`);
            return;
          }
          if (typeof properties.zone_id === "number") {
            selectZone.current(properties.zone_id);
          } else if (typeof properties.pair === "string") {
            selectPair.current(properties.pair);
          } else {
            back.current();
          }
        });
        const hover = new maplibregl.Popup({
          closeButton: false,
          closeOnClick: false,
          offset: 10,
          className: "zones-popup zones-node-hover",
        });
        let labelled: string | null = null;
        const labelLine = (pair: string | null) => {
          if (pair === labelled) return;
          labelled = pair;
          showLineLabel(created, pair);
        };
        // A hovered node marker shows its name, a hovered line between zones
        // its label, and the pointer shows over anything clickable.
        created.on("mousemove", (event) => {
          const [node] = created.queryRenderedFeatures(event.point, {
            layers: NODE_LAYERS,
          });
          const label = node?.properties?.label;
          if (typeof label === "string") {
            hover.setLngLat(event.lngLat).setText(label).addTo(created);
          } else {
            hover.remove();
          }
          const hit = at(event.point);
          const pair = hit?.properties?.pair;
          labelLine(
            LINE_LAYERS.includes(hit?.layer.id ?? "") &&
              typeof pair === "string"
              ? pair
              : null,
          );
          created.getCanvas().style.cursor =
            node !== undefined || hit !== undefined ? "pointer" : "";
        });
        created.on("mouseout", () => {
          hover.remove();
          labelLine(null);
        });
        window.sigsleuthMap = created; // For e2e tests and the devtools console.
      },
      setup: (created) => () => {
        if (window.sigsleuthMap === created) delete window.sigsleuthMap;
      },
    },
    [],
  );

  // Fit to the zones whenever new data arrives: a new window or channel may
  // be a different network.
  const fittedTo = useRef<NetworkZones | null>(null);
  useEffect(() => {
    if (map === null) return;
    for (const feature of data.relays.features) {
      const {
        icon,
        mark,
        size,
        warning,
        warning_width: width,
      } = feature.properties ?? {};
      const warningIcon = `${WARNING_ICON}-${width}`;
      if (warning && !map.hasImage(warningIcon)) {
        map.addImage(warningIcon, warningImage(width), { pixelRatio: 2 });
      }
      if (mark !== "warning" && !map.hasImage(icon)) {
        map.addImage(icon, relayImage(mark === "filled", size), {
          pixelRatio: 2,
        });
      }
    }
    map.getSource<maplibregl.GeoJSONSource>(RELAYS)?.setData(data.relays);
    map.getSource<maplibregl.GeoJSONSource>(ZONES)?.setData({
      type: "FeatureCollection",
      features: data.zones.features.map((feature) => {
        const { zone_id: zoneId, color } = feature.properties ?? {};
        const selected = zoneId === selectedZone;
        const icon = `zone-tag-${color}-${zoneId}-${selected}`;
        if (!map.hasImage(icon)) {
          map.addImage(icon, zoneTagImage(String(zoneId), color, selected), {
            pixelRatio: 2,
          });
        }
        return {
          ...feature,
          properties: { ...feature.properties, selected, icon },
        };
      }),
    });
    if (fittedTo.current !== network && data.bounds !== null) {
      map.fitBounds(data.bounds, { padding: 40, maxZoom: 11, animate: false });
      fittedTo.current = network;
    }
  }, [map, network, data, selectedZone]);

  // With a zone selected, every other zone with a figure gets a badge with the
  // share of the selected zone's texts that reached it.
  useEffect(() => {
    if (map === null) return;
    const reach = textReachLookup(network);
    const badges: GeoJSON.Feature[] = [];
    for (const feature of data.zones.features) {
      const { zone_id: zoneId } = feature.properties ?? {};
      if (selectedZone === null || zoneId === selectedZone) continue;
      if (feature.geometry.type !== "Point") continue;
      const figure = reach(selectedZone, zoneId as number);
      if (figure === null) continue;
      const share = figure === "no gateway" ? null : figure.share;
      const text = share === null ? "no gateway" : formatShare(share);
      const fill =
        share === null ? NO_GATEWAY_COLOR : SHARE_COLORS[tierOfShare(share)];
      const icon = `reach-${fill}-${text}`;
      if (!map.hasImage(icon)) {
        map.addImage(icon, badgeImage(text, fill), { pixelRatio: 2 });
      }
      // At the top right of the zone's tag.
      badges.push({
        type: "Feature",
        geometry: feature.geometry,
        properties: { icon, offset: [TAG_MIN_WIDTH / 2, -TAG_HEIGHT / 2] },
      });
    }
    map
      .getSource<maplibregl.GeoJSONSource>(REACH_BADGES)
      ?.setData({ type: "FeatureCollection", features: badges });
  }, [map, network, data, selectedZone]);

  // Zone-to-zone lines and their ends; the selected pair's line gives way to
  // its own links, and all of them to a zone's solid paths.
  const hideLines = pair === undefined && nodeLinks.length > 0;
  useEffect(() => {
    if (map === null) return;
    const shown = (collection: GeoJSON.FeatureCollection) => ({
      type: "FeatureCollection" as const,
      features: hideLines
        ? []
        : collection.features.filter(
            (feature) => feature.properties?.pair !== selectedPair,
          ),
    });
    map
      .getSource<maplibregl.GeoJSONSource>(ZONE_LINES)
      ?.setData(shown(data.lines));
    map
      .getSource<maplibregl.GeoJSONSource>(LINE_ENDS)
      ?.setData(shown(data.lineEnds));
  }, [map, data, selectedPair, hideLines]);

  // Members of the selected zone, or of both zones of the selected pair.
  useEffect(() => {
    if (map === null) return;
    const shown = new Set([selectedZone, pair?.zone_a, pair?.zone_b]);
    map.getSource<maplibregl.GeoJSONSource>(MEMBERS)?.setData({
      type: "FeatureCollection",
      features: data.members.features.filter((feature) =>
        shown.has(feature.properties?.zone_id as number),
      ),
    });
  }, [map, data, pair, selectedZone]);

  // The selected pair's links or the selected zone's solid paths, node to
  // node, and the nodes that carry them in their zones' colours.
  useEffect(() => {
    if (map === null) return;
    const lines: ReturnType<typeof linkHalves> = [];
    const ends = new Map<number, GeoJSON.Feature>();
    const end = (
      nodeId: number,
      point: [number, number],
      color: string | undefined,
    ) =>
      ends.set(nodeId, {
        type: "Feature",
        geometry: { type: "Point", coordinates: point },
        properties: {
          color: color ?? "#6b7570",
          node_id: nodeId,
          estimated: estimated.has(nodeId),
          label: hoverLabel(nodeId, nodes, estimated),
        },
      });
    const color = (nodeId: number) => colors.get(zoneOf.get(nodeId) ?? 0);
    for (const { link, a, b } of nodeLinks) {
      lines.push(...linkHalves(pairLinkKey(link), link, a, b));
      end(link.node_a, a, color(link.node_a));
      end(link.node_b, b, color(link.node_b));
    }
    map
      .getSource<maplibregl.GeoJSONSource>(PAIR_LINKS)
      ?.setData({ type: "FeatureCollection", features: lines });
    map
      .getSource<maplibregl.GeoJSONSource>(PAIR_ENDS)
      ?.setData({ type: "FeatureCollection", features: [...ends.values()] });
  }, [map, nodes, colors, estimated, zoneOf, nodeLinks]);

  // The selected relay's ring.
  useEffect(() => {
    if (map === null) return;
    map.getSource<maplibregl.GeoJSONSource>(SELECTED_RELAY)?.setData({
      type: "FeatureCollection",
      features: data.relays.features.filter(
        (feature) => feature.properties?.relay === selectedRelay,
      ),
    });
  }, [map, data, selectedRelay]);

  // Fit a zone's members, or centre one node, clicked beside the map.
  useEffect(() => {
    if (map === null || focus === null || focus.points.length === 0) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const point of focus.points) bounds.extend(point);
    map.fitBounds(bounds, { padding: 60, maxZoom: 12 });
  }, [map, focus]);

  // Zoom to the open link, else to all the selected pair's links. A zone's
  // solid paths can span the network, so selecting a zone does not zoom.
  const pairSelected = pair !== undefined;
  useEffect(() => {
    if (map === null) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const { link, a, b } of nodeLinks) {
      if (openLink === null ? pairSelected : pairLinkKey(link) === openLink) {
        bounds.extend(a).extend(b);
      }
    }
    if (!bounds.isEmpty()) {
      map.fitBounds(bounds, { padding: 60, maxZoom: 12 });
    }
  }, [map, nodeLinks, pairSelected, openLink]);

  // The open link is drawn alone and fully opaque, with only its two nodes.
  useEffect(() => {
    if (map === null) return;
    showLinkFocus(
      map,
      "traceroute",
      openLink === null ? null : [openLink],
      openLink,
    );
    const ends = openLink?.split("-").map(Number) ?? [];
    map.setFilter(
      `${PAIR_ENDS}-dots`,
      openLink === null ? null : ["in", ["get", "node_id"], ["literal", ends]],
    );
  }, [map, openLink]);

  return (
    <div className="zones-zone-map">
      <div className="zones-zone-map-canvas" ref={container} />
      {failure !== null ? (
        <p className="zones-map-message">{MAP_FAILURE_MESSAGES[failure]}</p>
      ) : data.bounds === null ? (
        <p className="zones-map-message">No zone has a position to show.</p>
      ) : null}
    </div>
  );
}

/** Labels lines with one solid link always, and one other line while hovered. */
function showLineLabel(map: maplibregl.Map, hovered: string | null) {
  map.setFilter(`${ZONE_LINES}-label`, [
    "any",
    ["get", "single"],
    ["==", ["get", "pair"], hovered ?? ""],
  ]);
}

function addLayers(map: maplibregl.Map) {
  for (const id of [
    ZONES,
    ZONE_LINES,
    LINE_ENDS,
    RELAYS,
    MEMBERS,
    PAIR_ENDS,
    REACH_BADGES,
    SELECTED_RELAY,
  ]) {
    map.addSource(id, { type: "geojson", data: EMPTY });
  }
  const polygons: maplibregl.FilterSpecification = [
    "==",
    ["geometry-type"],
    "Polygon",
  ];
  const points: maplibregl.FilterSpecification = [
    "==",
    ["geometry-type"],
    "Point",
  ];
  map.addLayer({
    id: `${ZONES}-fill`,
    type: "fill",
    source: ZONES,
    filter: polygons,
    paint: {
      "fill-color": ["get", "color"],
      "fill-opacity": ["case", ["get", "selected"], 0.45, 0.2],
    },
  });
  map.addLayer({
    id: `${ZONES}-outline`,
    type: "line",
    source: ZONES,
    filter: polygons,
    paint: {
      "line-color": ["get", "color"],
      "line-width": ["case", ["get", "selected"], 3, 1.5],
    },
  });
  // One layer per pattern: a data-driven dash pattern is not supported everywhere.
  for (const [style, dashes] of Object.entries(ROUTE_DASHES)) {
    map.addLayer({
      id: `${ZONE_LINES}-${style}`,
      type: "line",
      source: ZONE_LINES,
      filter: ["==", ["get", "route"], style],
      layout: { "line-cap": dashes === null ? "round" : "butt" },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["get", "width"],
        "line-opacity": 0.9,
        ...(dashes === null ? {} : { "line-dasharray": dashes }),
      },
    });
  }
  map.addLayer({
    id: `${LINE_ENDS}-dots`,
    type: "circle",
    source: LINE_ENDS,
    paint: {
      "circle-radius": 4,
      ...hollowWhenEstimated(1.5, 2),
    },
  });
  map.addLayer({
    id: `${PAIR_ENDS}-dots`,
    type: "circle",
    source: PAIR_ENDS,
    paint: {
      "circle-radius": 6,
      ...hollowWhenEstimated(2, 3),
    },
  });
  map.addLayer({
    id: SELECTED_RELAY,
    type: "circle",
    source: SELECTED_RELAY,
    paint: {
      // Just outside the disc or the bare mast.
      "circle-radius": ["+", ["*", ["get", "size"], 1.3], 4],
      "circle-opacity": 0,
      "circle-stroke-color": SELECTED_COLOR,
      "circle-stroke-width": 3,
    },
  });
  map.addLayer({
    id: `${RELAYS}-markers`,
    type: "symbol",
    source: RELAYS,
    filter: ["!=", ["get", "mark"], "warning"],
    layout: {
      "icon-image": ["get", "icon"],
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
      // Only solid links on top, then the biggest impact.
      "symbol-sort-key": [
        "+",
        ["get", "size"],
        ["case", ["==", ["get", "mark"], "filled"], 100, 0],
      ],
    },
  });
  // Warnings above the masts: at a mast's top right, or alone.
  map.addLayer({
    id: `${RELAYS}-warnings`,
    type: "symbol",
    source: RELAYS,
    filter: ["get", "warning"],
    layout: {
      "icon-image": ["concat", WARNING_ICON, "-", ["get", "warning_width"]],
      "icon-offset": ["get", "warning_offset"],
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
    },
  });
  map.addLayer({
    id: `${ZONE_LINES}-label`,
    type: "symbol",
    source: ZONE_LINES,
    filter: ["get", "single"],
    layout: {
      "symbol-placement": "line-center",
      "text-field": ["get", "label"],
      "text-size": 11,
      "text-font": ["Noto Sans Bold"],
    },
    paint: {
      "text-color": "#111",
      "text-halo-color": "#fff",
      "text-halo-width": 1.5,
    },
  });
  // The tags always show, and other zones' names keep clear of them. A name
  // goes below its tag, else to a side or above, else is left out; bigger
  // zones' names are placed first.
  map.addLayer({
    id: `${ZONES}-label`,
    type: "symbol",
    source: ZONES,
    filter: points,
    layout: {
      "icon-image": ["get", "icon"],
      "icon-allow-overlap": true,
      "symbol-sort-key": ["-", 0, ["get", "members"]],
      "text-field": ["get", "label"],
      "text-optional": true,
      "text-size": 12,
      "text-font": ["Noto Sans Bold"],
      "text-variable-anchor-offset": [
        "top",
        [0, 0.95],
        "left",
        [1.3, 0],
        "right",
        [-1.3, 0],
        "bottom",
        [0, -0.95],
      ],
      "text-justify": "auto",
      "text-max-width": 12,
    },
    paint: {
      "text-color": "#111",
      "text-halo-color": "#fff",
      "text-halo-width": 1.8,
    },
  });
  // The selected zone's members, above its tag, which sits among them and
  // would hide a small zone's members; edged in dark so grey zones' show.
  map.addLayer({
    id: `${MEMBERS}-dots`,
    type: "circle",
    source: MEMBERS,
    paint: {
      "circle-radius": 4,
      ...hollowWhenEstimated(1.5, 2),
      "circle-stroke-color": [
        "case",
        ["==", ["get", "estimated"], true],
        ["get", "color"],
        "#111",
      ],
    },
  });
  map.addLayer({
    id: REACH_BADGES,
    type: "symbol",
    source: REACH_BADGES,
    layout: {
      "icon-image": ["get", "icon"],
      "icon-anchor": "bottom-left",
      "icon-offset": ["get", "offset"],
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
    },
  });
  // Above the lines between zones, below the node markers; at every zoom, as
  // a pair's links may span a wide area.
  addLinkLayers(
    map,
    "traceroute",
    { type: "FeatureCollection", features: [] },
    `${LINE_ENDS}-dots`,
    0,
  );
}

/**
 * A node dot's colours: filled with the zone's colour and edged in white, or
 * at an estimated position hollow, white with a ring in the zone's colour.
 */
function hollowWhenEstimated(
  edge: number,
  ring: number,
): maplibregl.CircleLayerSpecification["paint"] {
  const estimated: maplibregl.ExpressionSpecification = [
    "==",
    ["get", "estimated"],
    true,
  ];
  return {
    "circle-color": ["case", estimated, "#fff", ["get", "color"]],
    "circle-stroke-color": ["case", estimated, ["get", "color"], "#fff"],
    "circle-stroke-width": ["case", estimated, ring, edge],
  };
}

/** Names one of a pair's links, for the open link's highlight. */
function pairLinkKey(link: { node_a: number; node_b: number }): string {
  return `${link.node_a}-${link.node_b}`;
}

/**
 * A key relay's mark, drawn at twice the screen resolution: a white mast on a
 * RELAY_COLOR disc with a white edge, or a RELAY_COLOR mast with a white halo.
 * `size` is about the disc's radius in pixels.
 */
function relayImage(filled: boolean, size: number) {
  const scale = 2;
  // A mast unit in image pixels; the mast reaches about 12.5 units out.
  const unit = (size * scale) / (filled ? 13 : 9.5);
  // From the centre to the disc's edge, or to the bare mast's farthest point.
  const reach = filled ? 1.1 * size * scale : 12.5 * unit;
  // The disc's white edge, or the white halo around the bare mast.
  const edge = (filled ? 1.5 : 3.5) * scale;
  const side = Math.ceil(2 * (reach + edge));
  const context = imageCanvas(side, side);
  context.translate(side / 2, side / 2);
  context.lineCap = "round";
  context.lineJoin = "round";
  if (filled) {
    context.beginPath();
    context.arc(0, 0, reach, 0, 2 * Math.PI);
    context.fillStyle = RELAY_COLOR;
    context.fill();
    context.lineWidth = edge;
    context.strokeStyle = "#fff";
    context.stroke();
    drawMast(context, unit, "#fff", 1.3 * scale);
  } else {
    drawMast(context, unit, "#fff", edge);
    drawMast(context, unit, RELAY_COLOR, 1.5 * scale);
  }
  return context.getImageData(0, 0, side, side);
}

/**
 * A key relay's warning, drawn at twice the screen resolution: a WARNING_COLOR
 * triangle edged in white with a dark "!", `size` pixels wide.
 */
function warningImage(size: number) {
  const scale = 2;
  const edge = 1.5 * scale;
  const side = Math.ceil((size + 4) * scale);
  const context = imageCanvas(side, side);
  const width = size * scale;
  const height = width * 0.88;
  const top = (side - height) / 2;
  const left = (side - width) / 2;
  context.lineJoin = "round";
  context.beginPath();
  context.moveTo(side / 2, top);
  context.lineTo(left + width, top + height);
  context.lineTo(left, top + height);
  context.closePath();
  context.fillStyle = WARNING_COLOR;
  context.fill();
  context.lineWidth = edge;
  context.strokeStyle = "#fff";
  context.stroke();
  context.fillStyle = "#1f1a10";
  context.font = `bold ${Math.round(0.6 * width)}px 'Noto Sans', sans-serif`;
  context.textAlign = "center";
  context.textBaseline = "alphabetic";
  context.fillText("!", side / 2, top + height - 0.12 * width);
  return context.getImageData(0, 0, side, side);
}

/** Strokes RELAY_MAST `width` pixels wide, `unit` pixels to a mast unit. */
function drawMast(
  context: CanvasRenderingContext2D,
  unit: number,
  color: string,
  width: number,
) {
  context.save();
  context.scale(unit, unit);
  context.strokeStyle = color;
  context.fillStyle = color;
  context.lineWidth = width / unit;
  const tip = new Path2D(RELAY_MAST_TIP);
  context.stroke(new Path2D(RELAY_MAST));
  context.stroke(tip);
  context.fill(tip);
  context.restore();
}

/**
 * A zone's tag, drawn at twice the screen resolution: its number in white on
 * its colour, edged in white, or in near-black when the zone is selected.
 */
function zoneTagImage(text: string, fill: string, selected: boolean) {
  const scale = 2;
  const font = `bold ${11 * scale}px 'Noto Sans', sans-serif`;
  const measure = imageCanvas(1, 1);
  measure.font = font;
  const height = TAG_HEIGHT * scale;
  const width = Math.max(
    Math.ceil(measure.measureText(text).width) + 10 * scale,
    TAG_MIN_WIDTH * scale,
  );
  const edge = (selected ? 2.5 : 1.5) * scale;
  const context = imageCanvas(width, height);
  context.beginPath();
  context.roundRect(edge / 2, edge / 2, width - edge, height - edge, 5 * scale);
  context.fillStyle = fill;
  context.fill();
  context.lineWidth = edge;
  context.strokeStyle = selected ? "#111" : "#fff";
  context.stroke();
  context.font = font;
  context.fillStyle = "#fff";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(text, width / 2, height / 2 + scale);
  return context.getImageData(0, 0, width, height);
}

interface MapFeatures {
  zones: GeoJSON.FeatureCollection;
  lines: GeoJSON.FeatureCollection;
  /** The nodes at the ends of the lines, with their line's pair. */
  lineEnds: GeoJSON.FeatureCollection;
  relays: GeoJSON.FeatureCollection;
  members: GeoJSON.FeatureCollection;
  bounds: maplibregl.LngLatBounds | null;
}

/**
 * A node's name (long, else short) and hex ID, or just the ID, then `more`,
 * and whether its position is an estimate.
 */
function hoverLabel(
  nodeId: number,
  nodes: Map<number, NodeSummary>,
  estimated: Set<number>,
  ...more: string[]
): string {
  const name = nodeName(nodes.get(nodeId));
  return [
    ...(name ? [name] : []),
    formatNodeId(nodeId),
    ...more,
    ...(estimated.has(nodeId) ? ["estimated position"] : []),
  ].join(" · ");
}

function mapFeatures(
  network: NetworkZones,
  nodes: Map<number, NodeSummary>,
  colors: Map<number, string>,
  names: Map<number, string>,
): MapFeatures {
  const estimated = estimatedNodes(network);
  const zones: GeoJSON.Feature[] = [];
  const members: GeoJSON.Feature[] = [];
  const bounds = new maplibregl.LngLatBounds();
  const tags = new Map<number, [number, number]>();
  const zoneOf = new Map<number, number>();
  for (const zone of network.zones) {
    for (const id of zone.members) zoneOf.set(id, zone.zone_id);
    const color = colors.get(zone.zone_id) ?? "#6b7570";
    // The area covers reported positions only; estimates still get a dot.
    const points = zone.members
      .filter((id) => !estimated.has(id))
      .map((id) => placed(nodes.get(id)))
      .filter((point) => point !== null);
    for (const id of zone.members) {
      const point = placed(nodes.get(id));
      if (point === null) continue;
      members.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: point },
        properties: {
          zone_id: zone.zone_id,
          color,
          node_id: id,
          estimated: estimated.has(id),
          label: hoverLabel(id, nodes, estimated),
        },
      });
    }
    if (zone.latitude === null || zone.longitude === null) continue;
    const properties = {
      zone_id: zone.zone_id,
      color,
      members: zone.members.length,
      label: `${names.get(zone.zone_id) ?? zone.name} (${zone.members.length})`,
    };
    const hull = zoneHull(points);
    let tag: [number, number] = [zone.longitude, zone.latitude];
    if (hull.length >= 3) {
      zones.push({
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [[...hull, hull[0]]] },
        properties,
      });
      hull.forEach((point) => bounds.extend(point));
      tag = hullCentre(hull) ?? tag;
    }
    tags.set(zone.zone_id, tag);
    zones.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: tag },
      properties,
    });
    bounds.extend(tag);
  }

  const lines: GeoJSON.Feature[] = [];
  const lineEnds: GeoJSON.Feature[] = [];
  for (const pair of network.zone_pairs) {
    const a = tags.get(pair.zone_a);
    const b = tags.get(pair.zone_b);
    const margin = pair.strongest_margin_db;
    // Zones joined only through other zones get no line.
    if (a === undefined || b === undefined || margin === null) continue;
    const links = pair.links.length;
    const route: RouteStyle = pair.solid_links === 0 ? "dashed" : "solid";
    const through = mostSolidLink(pair, nodes);
    lines.push({
      type: "Feature",
      geometry: {
        type: "LineString",
        coordinates: through === null ? [a, b] : [through.a, through.b],
      },
      properties: {
        pair: pairKey(pair),
        route,
        single: pair.solid_links === 1,
        color: linkTierColor(marginTier(margin)),
        width: pair.solid_links >= 2 ? WIDE_LINE : THIN_LINE,
        label: [
          `${links} link${links === 1 ? "" : "s"}`,
          `${margin.toFixed(0)} dB`,
          `${pair.hub_hops ?? "?"} hops`,
        ].join(" · "),
      },
    });
    if (through === null) continue;
    for (const [nodeId, point] of [
      [through.link.node_a, through.a],
      [through.link.node_b, through.b],
    ] as const) {
      lineEnds.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: point },
        properties: {
          pair: pairKey(pair),
          color: colors.get(zoneOf.get(nodeId) ?? 0) ?? "#6b7570",
          node_id: nodeId,
          estimated: estimated.has(nodeId),
          label: hoverLabel(nodeId, nodes, estimated),
        },
      });
    }
  }

  const relays: GeoJSON.Feature[] = [];
  const now = Date.parse(network.generated_at);
  for (const relay of network.key_relays) {
    const point = placed(nodes.get(relay.node_id));
    const mark = relayMark(relay, now, network.rules);
    if (mark === null || point === null) continue;
    const concern = relayConcern(relay, now);
    // More zones joined alone, or more nodes affected, make a bigger marker,
    // from 6 to 10 so the biggest do not bury their neighbours; whole pixels,
    // as each size is an image. A warning alone is about as big as the
    // smallest mast.
    const size =
      mark === "warning"
        ? 6
        : Math.round(
            Math.min(
              Math.max(
                5 + Math.sqrt(relay.impact),
                mark === "filled"
                  ? 6 + 1.5 * relay.only_solid_link_to.length
                  : 0,
              ),
              10,
            ),
          );
    // Over a mast, the triangle grows with it and sits at its top right.
    const warningWidth = mark === "warning" ? LONE_WARNING_WIDTH : size + 10;
    const offset = mark === "warning" ? 0 : size + 0.25 * warningWidth;
    relays.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: point },
      properties: {
        icon: `${RELAYS}-${mark}-${size}`,
        mark,
        size,
        warning: concern !== null,
        warning_width: warningWidth,
        warning_offset: [offset, -offset],
        relay: relay.node_id,
        label: hoverLabel(
          relay.node_id,
          nodes,
          estimated,
          relayReason(relay, names, network.rules),
          ...(concern === null ? [] : [concern]),
        ),
      },
    });
  }

  return {
    zones: { type: "FeatureCollection", features: zones },
    lines: { type: "FeatureCollection", features: lines },
    lineEnds: { type: "FeatureCollection", features: lineEnds },
    relays: { type: "FeatureCollection", features: relays },
    members: { type: "FeatureCollection", features: members },
    bounds: bounds.isEmpty() ? null : bounds,
  };
}
