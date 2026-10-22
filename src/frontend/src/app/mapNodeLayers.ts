// One clustered GeoJSON source gives links, labels and badges the same node
// positions without a DOM button per marker. View properties carry selection
// and focus together; style reloads can reinstall the same layer expressions.
import type { FeatureCollection, Point } from "geojson";
import type {
  ExpressionSpecification,
  GeoJSONSource,
  Map as MapLibreMap,
} from "maplibre-gl";

import type { MapNode } from "./api";
import { nodeMarkerLabel } from "./nodeDisplay";

interface NodeProperties {
  nodeId: number;
  label: string;
  category: NodeCategory;
  /** Outside the focus a node is hidden. */
  shown: boolean;
  selected: boolean;
  /** The gateway's share of the selected node's texts, such as "90%", or "". */
  badge: string;
  /** The badge's image, with the share drawn on it in its tier's colour, or "". */
  badgeIcon: string;
  /** Drawn at an estimate, as a hollow marker. */
  estimated: boolean;
}

export type NodeFeatureCollection = FeatureCollection<Point, NodeProperties>;

/**
 * What a marker's colour says: routers and repeaters relay for others, and
 * gateways report what they hear to MQTT. A router that is also a gateway
 * counts as a relay.
 */
export type NodeCategory = "relay" | "gateway" | "other";

export const NODE_CATEGORIES: readonly NodeCategory[] = [
  "relay",
  "gateway",
  "other",
];

// Chosen to stand out on every basemap (green parks, blue water, brown
// contours, dark land). The gateway green is brighter than the good-link green.
export const NODE_COLORS: Record<NodeCategory, string> = {
  relay: "#1d4ed8",
  gateway: "#22c55e",
  other: "#c026d3",
};
const RELAY_ROLES = new Set([
  "ROUTER",
  "ROUTER_LATE",
  "ROUTER_CLIENT",
  "REPEATER",
]);

/** A gateway is a node that has reported a reception in retained history. */
export function nodeCategory(node: MapNode): NodeCategory {
  if (RELAY_ROLES.has(node.role ?? "")) return "relay";
  return node.last_gateway_at ? "gateway" : "other";
}

/** What the map shows beyond the nodes themselves. */
export interface NodeView {
  /**
   * The nodes that stay, such as those within the hop depth or a selected
   * link's two ends; null keeps every node. Gateways in shares stay too.
   */
  focus: ReadonlySet<number> | null;
  selectedId: number | null;
  /** The selected node's gateways, each badged with its share. */
  shares: readonly TextShare[];
}

export const NO_NODE_VIEW: NodeView = {
  focus: null,
  selectedId: null,
  shares: [],
};

export function buildNodeFeatureCollection(
  items: MapNode[],
  { focus, selectedId, shares }: NodeView = NO_NODE_VIEW,
): NodeFeatureCollection {
  const sharesById = new Map(shares.map((share) => [share.nodeId, share]));
  return {
    type: "FeatureCollection",
    features: items.map((node) => {
      const share = sharesById.get(node.node_id);
      return {
        type: "Feature",
        geometry: {
          type: "Point",
          coordinates: [node.longitude, node.latitude],
        },
        properties: {
          nodeId: node.node_id,
          label: nodeMarkerLabel(node),
          category: nodeCategory(node),
          shown:
            focus === null || focus.has(node.node_id) || share !== undefined,
          selected: node.node_id === selectedId,
          badge:
            share === undefined
              ? ""
              : textShareLabel(share.receptions, share.messages),
          badgeIcon:
            share === undefined
              ? ""
              : `share-${shareTier(share.receptions, share.messages)}-${textShareLabel(share.receptions, share.messages)}`,
          estimated: node.estimate !== null,
        },
      };
    }),
  };
}

const NOT_CLUSTER: ExpressionSpecification = ["!", ["has", "point_count"]];
// A single node outside the focus is hidden on every layer.
const SHOWN_NODE: ExpressionSpecification = [
  "all",
  NOT_CLUSTER,
  ["boolean", ["get", "shown"], false],
];

/**
 * Every layer drawn for a single node: its rim, dot, name, share badge and
 * star. A click or hover on any of them is on the node, not on empty map.
 */
export const NODE_LAYERS = [
  "node-halos",
  "nodes",
  "node-labels",
  "node-shares",
  "node-selected",
];

/** The node layers that take clicks: with coverage on, names leave them to the cells. */
export function clickableNodeLayers(coverage: boolean): string[] {
  return NODE_LAYERS.filter((layer) => !coverage || layer !== "node-labels");
}

const estimated: ExpressionSpecification = [
  "boolean",
  ["get", "estimated"],
  false,
];
// Relays draw over gateways and gateways over other nodes, and their names win
// a collision, so a lesser node never hides a relay. Within a category real
// positions draw over estimated ones, so an estimate never hides a reported
// position. Higher ranks draw on top.
const markerRank: ExpressionSpecification = [
  "+",
  ["match", ["get", "category"], "relay", 4, "gateway", 2, 0],
  ["case", estimated, 0, 1],
];
const hovered: ExpressionSpecification = [
  "boolean",
  ["feature-state", "hover"],
  false,
];
const selected: ExpressionSpecification = [
  "boolean",
  ["get", "selected"],
  false,
];

// Dots grow with the zoom, and a little more when hovered or selected. The
// halo passes extra to sit just outside the dot's white ring.
function dotRadius(extra = 0): ExpressionSpecification {
  const stop = (radius: number): ExpressionSpecification => [
    "case",
    ["any", hovered, selected],
    radius + 2 + extra,
    radius + extra,
  ];
  return [
    "interpolate",
    ["linear"],
    ["zoom"],
    5,
    stop(4),
    9,
    stop(6),
    13,
    stop(8),
  ];
}

export function addNodeLayers(
  map: MapLibreMap,
  data: NodeFeatureCollection,
  { darkLabels = true, orangeClusters = false } = {},
): void {
  map.addSource("nodes", {
    type: "geojson",
    data,
    cluster: true,
    clusterRadius: 35,
    clusterMaxZoom: 4,
    promoteId: "nodeId",
  });

  // Clusters have a slate ring, so they do not read as a node. They are white,
  // or orange on pale basemaps where white would fade into the land.
  map.addLayer({
    id: "node-clusters",
    type: "circle",
    source: "nodes",
    filter: ["has", "point_count"],
    paint: {
      "circle-radius": [
        "step",
        ["get", "point_count"],
        11,
        10,
        13,
        50,
        16,
        150,
        19,
      ],
      "circle-color": orangeClusters
        ? ["case", hovered, "#ea580c", "#f97316"]
        : ["case", hovered, "#e2e8f0", "#ffffff"],
      "circle-stroke-color": "#334155",
      "circle-stroke-width": 3,
    },
  });

  map.addLayer({
    id: "cluster-count",
    type: "symbol",
    source: "nodes",
    filter: ["has", "point_count"],
    layout: {
      "text-field": ["get", "point_count"],
      "text-font": ["Noto Sans Bold"],
      "text-size": 11,
      "text-allow-overlap": true,
    },
    paint: { "text-color": "#1e293b" },
  });

  // A thin dark rim around each dot keeps its white ring visible on pale basemaps.
  map.addLayer({
    id: "node-halos",
    type: "circle",
    source: "nodes",
    filter: SHOWN_NODE,
    layout: { "circle-sort-key": markerRank },
    paint: {
      "circle-radius": dotRadius(2.5),
      "circle-color": "rgb(15 23 42 / 70%)",
    },
  });

  map.addLayer({
    id: "nodes",
    type: "circle",
    source: "nodes",
    filter: SHOWN_NODE,
    layout: { "circle-sort-key": markerRank },
    paint: {
      "circle-radius": dotRadius(),
      // An estimated node is hollow: white with a ring in its colour.
      "circle-color": ["case", estimated, "#ffffff", categoryColor],
      "circle-stroke-color": [
        "case",
        selected,
        "#f5a623",
        estimated,
        categoryColor,
        "#ffffff",
      ],
      "circle-stroke-width": ["case", estimated, 3, 1.5],
    },
  });

  // Short names sit beside the dot, on whichever side has room; MapLibre
  // leaves out a name that would overlap another, so zooming in shows more.
  map.addLayer({
    id: "node-labels",
    type: "symbol",
    source: "nodes",
    filter: SHOWN_NODE,
    layout: {
      "text-field": ["get", "label"],
      "text-font": ["Noto Sans Bold"],
      "text-size": 11,
      "text-variable-anchor": ["left", "right", "top", "bottom"],
      "text-radial-offset": 0.8,
      // Lower keys are placed first, so the higher-ranked node keeps its name.
      "symbol-sort-key": ["-", 5, markerRank],
    },
    paint: {
      "text-color": darkLabels ? "#1e293b" : "#f8fafc",
      "text-halo-color": darkLabels ? "#ffffff" : "#0f172a",
      "text-halo-width": 1.5,
    },
  });

  addBadgeImages(map, data);
  addShareLayer(map);
  addSelectedLayer(map);
}

const categoryColor: ExpressionSpecification = [
  "match",
  ["get", "category"],
  "relay",
  NODE_COLORS.relay,
  "gateway",
  NODE_COLORS.gateway,
  NODE_COLORS.other,
];

/** One gateway's share of a sender's broadcast text messages. */
export interface TextShare {
  nodeId: number;
  receptions: number;
  messages: number;
}

export type ShareTier = "high" | "medium" | "low" | "poor";

/** 80% and up is high, 50% and up medium, 20% or less poor, the rest low. */
export function shareTier(receptions: number, messages: number): ShareTier {
  return tierOfShare(messages === 0 ? 0 : receptions / messages);
}

/** The tier of a share between 0 and 1. */
export function tierOfShare(share: number): ShareTier {
  if (share >= 0.8) return "high";
  if (share >= 0.5) return "medium";
  return share > 0.2 ? "low" : "poor";
}

/** Such as "90%"; the Chatty nodes list has the message count. */
export function textShareLabel(receptions: number, messages: number): string {
  const percent =
    messages === 0 ? 0 : Math.round((100 * receptions) / messages);
  return `${percent}%`;
}

// Solid badges with dark text read on every basemap, so one set of colours
// serves them all.
export const SHARE_COLORS: Record<ShareTier, string> = {
  high: "#4ade80",
  medium: "#facc15",
  low: "#fb923c",
  poor: "#f87171",
};

/** A canvas of the given size for drawing a map image. */
export function imageCanvas(width: number, height: number) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas.getContext("2d")!;
}

/**
 * A rounded badge with a dark border and the share written on it, drawn at
 * twice the screen resolution. The number is part of the image because
 * MapLibre draws a layer's text above all of its icons: a badge underneath
 * would show its number through the one on top.
 */
export function badgeImage(text: string, fill: string) {
  const font = "bold 22px 'Noto Sans', sans-serif";
  const measure = imageCanvas(1, 1);
  measure.font = font;
  const width = Math.ceil(measure.measureText(text).width) + 18;
  const height = 34;
  const context = imageCanvas(width, height);
  context.beginPath();
  context.roundRect(1, 1, width - 2, height - 2, 7);
  context.fillStyle = fill;
  context.fill();
  context.lineWidth = 2;
  context.strokeStyle = "#1e293b";
  context.stroke();
  context.font = font;
  context.fillStyle = "#1e293b";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(text, width / 2, height / 2 + 1);
  return context.getImageData(0, 0, width, height);
}

/** Adds the image of every badge in the data that the map does not have yet. */
function addBadgeImages(map: MapLibreMap, data: NodeFeatureCollection): void {
  for (const { properties } of data.features) {
    const { badge, badgeIcon } = properties;
    if (badgeIcon === "" || map.hasImage(badgeIcon)) continue;
    const tier = badgeIcon.split("-")[1] as ShareTier;
    map.addImage(badgeIcon, badgeImage(badge, SHARE_COLORS[tier]), {
      pixelRatio: 2,
    });
  }
}

function addShareLayer(map: MapLibreMap): void {
  // Added above the short names and placed first, so names move aside for a
  // badge; badges always show, and where they overlap the one on top hides
  // the ones below.
  map.addLayer({
    id: "node-shares",
    type: "symbol",
    source: "nodes",
    filter: ["all", SHOWN_NODE, ["!=", ["get", "badge"], ""]],
    layout: {
      "icon-image": ["get", "badgeIcon"],
      "icon-anchor": "bottom-left",
      "icon-offset": [5, -5],
      "icon-allow-overlap": true,
    },
  });
}

/** A black star with a white outline, drawn at twice the screen resolution. */
function starImage() {
  const size = 60;
  const context = imageCanvas(size, size);
  context.beginPath();
  for (let index = 0; index < 10; index++) {
    const radius = index % 2 === 0 ? 27 : 11;
    const angle = (index * Math.PI) / 5 - Math.PI / 2;
    context.lineTo(
      size / 2 + radius * Math.cos(angle),
      size / 2 + 1 + radius * Math.sin(angle),
    );
  }
  context.closePath();
  context.fillStyle = "#000000";
  context.fill();
  // Only the inner half of the stroke shows, so the outline stays inside.
  context.clip();
  context.lineWidth = 6;
  context.strokeStyle = "#ffffff";
  context.stroke();
  return context.getImageData(0, 0, size, size);
}

// The selected node's star sits above every other node layer, so it shows
// even where neighbouring dots and names crowd it.
function addSelectedLayer(map: MapLibreMap): void {
  if (!map.hasImage("selected-star"))
    map.addImage("selected-star", starImage(), { pixelRatio: 2 });
  map.addLayer({
    id: "node-selected",
    type: "symbol",
    source: "nodes",
    filter: ["all", SHOWN_NODE, selected],
    layout: {
      "icon-image": "selected-star",
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
    },
  });
}

/**
 * Names every shown node, or with `named` only those and the selected node,
 * such as the nodes heard in view while coverage is on. A filter, so the
 * node data stays as it is.
 */
export function showNodeNames(
  map: MapLibreMap,
  named: readonly number[] | null,
): void {
  if (map.getLayer("node-labels") === undefined) return;
  map.setFilter(
    "node-labels",
    named === null
      ? SHOWN_NODE
      : [
          "all",
          SHOWN_NODE,
          ["any", selected, ["in", ["get", "nodeId"], ["literal", named]]],
        ],
  );
}

/**
 * The only writer to the node layers: shows the nodes with their focus,
 * selection and badges, and hides the clusters while a focus is active. Nodes
 * still inside a cluster at low zoom stay hidden until the map zooms in, as
 * their links do.
 */
export function applyNodeView(
  map: MapLibreMap,
  data: NodeFeatureCollection,
  focusActive: boolean,
): void {
  addBadgeImages(map, data);
  (map.getSource("nodes") as GeoJSONSource | undefined)?.setData(data);
  for (const layer of ["node-clusters", "cluster-count"])
    if (map.getLayer(layer) !== undefined)
      map.setLayoutProperty(
        layer,
        "visibility",
        focusActive ? "none" : "visible",
      );
}
