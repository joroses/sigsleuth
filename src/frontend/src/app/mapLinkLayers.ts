import type { FeatureCollection, LineString, Point, Position } from "geojson";
import type { ExpressionSpecification, Map } from "maplibre-gl";

import type { LinkKind, MapLink, MapNode, ZeroCostHop } from "./api";
import { type LatLon, distanceMeters } from "./lineOfSight";
import {
  type LinkTier,
  linkTier,
  linkTierColor,
  linkWidth,
} from "./linkQuality";
import { arrowRotation } from "./minimapMarkers";

interface LinkHalfProperties {
  linkKey: string;
  nodeA: number;
  nodeB: number;
  /** What the node at this half's end hears from the other node. */
  tier: LinkTier;
  color: string;
  width: number;
}

export type LinkFeatureCollection = FeatureCollection<
  LineString,
  LinkHalfProperties
>;

export const LINK_KINDS: LinkKind[] = ["traceroute", "direct", "neighbor"];

// Each link kind has its own source and two line layers: one coloured
// by quality and one for halves without an SNR.
export function linkSourceId(linkKind: LinkKind): string {
  return `links-${linkKind}`;
}

export function noSnrLayerId(linkKind: LinkKind): string {
  return `links-${linkKind}-no-snr`;
}

export const LINK_LAYERS = LINK_KINDS.flatMap((linkKind) => [
  linkSourceId(linkKind),
  noSnrLayerId(linkKind),
]);

// Clusters end at zoom 4, so from zoom 5 every line ends at a marker.
export const LINK_MIN_ZOOM = 5;
const LINK_OPACITY = 0.55;
const NO_SNR_OPACITY = 0.4;

// Traceroute links are solid, or dashed without an SNR. Direct links are
// dotted: zero-length dashes with round caps draw dots one line width across,
// spaced wider without an SNR. Neighbour links alternate a dash and a dot the
// same way. Dash lengths are in line widths, so dotted lines are drawn wider
// to keep thin ones' dots visible.
const LINE_STYLES: Record<
  LinkKind,
  {
    dashes: number[] | null;
    noSnrDashes: number[];
    noSnrCap: "butt" | "round";
    extraWidth: number;
  }
> = {
  traceroute: {
    dashes: null,
    noSnrDashes: [2, 2],
    noSnrCap: "butt",
    extraWidth: 0,
  },
  direct: {
    dashes: [0, 2],
    noSnrDashes: [0, 4],
    noSnrCap: "round",
    extraWidth: 1.5,
  },
  neighbor: {
    dashes: [2.5, 2.5, 0, 2.5],
    noSnrDashes: [2.5, 4, 0, 4],
    noSnrCap: "round",
    extraWidth: 1,
  },
};

/**
 * Unique across all link kinds and every channel, so a selection
 * names one line.
 */
export function linkKey(linkKind: LinkKind, link: MapLink): string {
  return `${linkKind}:${link.node_a}-${link.node_b}:${link.channel}`;
}

/** What a link needs to be drawn as two halves; zone pair links have it too. */
export type HalvedLink = Pick<
  MapLink,
  "node_a" | "node_b" | "spreading_factor" | "a_to_b" | "b_to_a"
>;

/**
 * Two half-lines per link that meet at the on-screen midpoint. The half nearest
 * B shows a_to_b, what B hears from A; the half nearest A shows b_to_a. A link
 * is drawn only when both nodes are on the map.
 */
export function buildLinkFeatureCollection(
  linkKind: LinkKind,
  links: MapLink[],
  nodesById: ReadonlyMap<number, MapNode>,
): LinkFeatureCollection {
  const features: LinkFeatureCollection["features"] = [];
  for (const link of links) {
    const a = nodesById.get(link.node_a);
    const b = nodesById.get(link.node_b);
    if (a === undefined || b === undefined) continue;
    features.push(
      ...linkHalves(
        linkKey(linkKind, link),
        link,
        [a.longitude, a.latitude],
        [b.longitude, b.latitude],
      ),
    );
  }
  return { type: "FeatureCollection", features };
}

/** The two halves of one link drawn from start (node A) to end (node B). */
export function linkHalves(
  key: string,
  link: HalvedLink,
  start: Position,
  end: Position,
): LinkFeatureCollection["features"] {
  const middle = mercatorMidpoint(start, end);
  const shared = {
    linkKey: key,
    nodeA: link.node_a,
    nodeB: link.node_b,
    width: linkWidth(link.a_to_b.observations + link.b_to_a.observations),
  };
  const [startTier, endTier] = linkHalfTiers(link);
  const halves: [Position[], LinkTier][] = [
    [[start, middle], startTier],
    [[middle, end], endTier],
  ];
  return halves.map(([coordinates, tier]) => ({
    type: "Feature",
    geometry: { type: "LineString", coordinates },
    properties: { ...shared, tier, color: linkTierColor(tier) },
  }));
}

/** The tiers of a link's halves: nearest A (b_to_a), then nearest B (a_to_b). */
export function linkHalfTiers(link: HalvedLink): [LinkTier, LinkTier] {
  return [
    linkTier(link.b_to_a.average_snr, link.spreading_factor),
    linkTier(link.a_to_b.average_snr, link.spreading_factor),
  ];
}

// The map draws lines straight in Web Mercator, so the halves meet at the
// Mercator midpoint. Crossing the antimeridian is not handled.
function mercatorMidpoint(start: Position, end: Position): Position {
  const toY = (latitude: number) =>
    Math.log(Math.tan(Math.PI / 4 + (latitude * Math.PI) / 360));
  const y = (toY(start[1]) + toY(end[1])) / 2;
  const latitude = ((2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180) / Math.PI;
  return [(start[0] + end[0]) / 2, latitude];
}

/**
 * Adds one kind's link layers under beforeId, when given, so markers stay on
 * top and clickable. They show from minZoom, LINK_MIN_ZOOM unless given.
 */
export function addLinkLayers(
  map: Map,
  linkKind: LinkKind,
  data: LinkFeatureCollection,
  beforeId: string | undefined,
  minZoom = LINK_MIN_ZOOM,
): void {
  const source = linkSourceId(linkKind);
  const style = LINE_STYLES[linkKind];
  map.addSource(source, { type: "geojson", data });
  map.addLayer(
    {
      id: source,
      type: "line",
      source,
      minzoom: minZoom,
      layout: { "line-cap": "round" },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["+", ["get", "width"], style.extraWidth],
        "line-opacity": LINK_OPACITY,
        ...(style.dashes !== null ? { "line-dasharray": style.dashes } : {}),
      },
    },
    beforeId,
  );
  map.addLayer(
    {
      id: noSnrLayerId(linkKind),
      type: "line",
      source,
      minzoom: minZoom,
      layout: { "line-cap": style.noSnrCap },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["+", ["get", "width"], style.extraWidth],
        "line-opacity": NO_SNR_OPACITY,
        "line-dasharray": style.noSnrDashes,
      },
    },
    beforeId,
  );
  showLinkFocus(map, linkKind, null, null);
}

/** The 0-cost badge's blue (--blue), for what MapLibre draws. */
export function zeroCostColor(): string {
  return getComputedStyle(document.documentElement)
    .getPropertyValue("--blue")
    .trim();
}

export const ZERO_COST_LAYER = "links-zero-cost";
export const ARROW_IMAGE = "hop-arrow";
// Pixels each of two arrowheads moves forward, so they sit back to back.
const BACK_TO_BACK_OFFSET = 5;

interface ZeroCostArrowProperties {
  linkKey: string;
  rotation: number;
  offset: [number, number];
}

export type ZeroCostArrows = FeatureCollection<Point, ZeroCostArrowProperties>;

/**
 * An arrowhead at the middle of each drawn traceroute link for each of its
 * 0-cost directions (zero_cost_hops), pointing the way the copies went;
 * free both ways, two back to back.
 */
export function zeroCostArrows(
  links: MapLink[],
  hops: ZeroCostHop[],
  nodesById: ReadonlyMap<number, MapNode>,
): ZeroCostArrows {
  const free = new Set(
    hops.map((hop) => `${hop.from_node_id}-${hop.to_node_id}:${hop.channel}`),
  );
  const features: ZeroCostArrows["features"] = [];
  for (const link of links) {
    const ways = [
      [link.node_a, link.node_b],
      [link.node_b, link.node_a],
    ].filter(([from, to]) => free.has(`${from}-${to}:${link.channel}`));
    const a = nodesById.get(link.node_a);
    const b = nodesById.get(link.node_b);
    if (ways.length === 0 || a === undefined || b === undefined) continue;
    const at = mercatorMidpoint(
      [a.longitude, a.latitude],
      [b.longitude, b.latitude],
    );
    for (const [from, to] of ways) {
      const start = from === a.node_id ? a : b;
      const end = to === a.node_id ? a : b;
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: at },
        properties: {
          linkKey: linkKey("traceroute", link),
          rotation: arrowRotation(
            [start.longitude, start.latitude],
            [end.longitude, end.latitude],
          ),
          offset: [ways.length === 2 ? BACK_TO_BACK_OFFSET : 0, 0],
        },
      });
    }
  }
  return { type: "FeatureCollection", features };
}

/**
 * The 0-cost arrowheads over the traceroute links, under beforeId, from
 * LINK_MIN_ZOOM like the links. Not clickable: the line under each is.
 */
export function addZeroCostLayer(
  map: Map,
  data: ZeroCostArrows,
  beforeId: string | undefined,
): void {
  addArrowImage(map);
  map.addSource(ZERO_COST_LAYER, { type: "geojson", data });
  map.addLayer(
    {
      id: ZERO_COST_LAYER,
      type: "symbol",
      source: ZERO_COST_LAYER,
      minzoom: LINK_MIN_ZOOM,
      layout: {
        "icon-image": ARROW_IMAGE,
        "icon-rotate": ["get", "rotation"],
        "icon-rotation-alignment": "map",
        "icon-offset": ["get", "offset"],
        // Larger than a route minimap's: map lines are thinner and many.
        "icon-size": 1.25,
        "icon-allow-overlap": true,
        "icon-ignore-placement": true,
      },
      paint: {
        "icon-color": zeroCostColor(),
        "icon-halo-color": ARROW_HALO,
        "icon-halo-width": 1,
      },
    },
    beforeId,
  );
}

// A dark edge keeps the light blue visible on light basemaps.
export const ARROW_HALO = "#0f172a";

export function addArrowImage(map: Map): void {
  if (!map.hasImage(ARROW_IMAGE))
    map.addImage(ARROW_IMAGE, arrowImage(), { sdf: true, pixelRatio: 2 });
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

export const MAX_HOP_DEPTH = 7;

/**
 * The nodes and links within `depth` relays of a node, walking the given links
 * both ways, whatever their direction or link kind. Depth 0 keeps the node's own
 * links and the nodes at their other end; each step adds one relay, so nodes up
 * to depth + 1 links away are kept. Chained links need not be one real path:
 * they may come from different hours and directions.
 */
export function withinHops(
  nodeId: number,
  links: { linkKind: LinkKind; link: MapLink }[],
  depth: number,
): { nodeIds: number[]; linkKeys: string[] } {
  // globalThis.Map, because Map in this file is MapLibre's map.
  const neighbours = new globalThis.Map<number, number[]>();
  const connect = (from: number, to: number) => {
    const list = neighbours.get(from);
    if (list === undefined) neighbours.set(from, [to]);
    else list.push(to);
  };
  for (const { link } of links) {
    connect(link.node_a, link.node_b);
    connect(link.node_b, link.node_a);
  }
  // Breadth-first, so each node's distance is its fewest links from nodeId.
  const distance = new globalThis.Map([[nodeId, 0]]);
  let frontier = [nodeId];
  for (let step = 1; step <= depth + 1 && frontier.length > 0; step++) {
    const next: number[] = [];
    for (const node of frontier)
      for (const neighbour of neighbours.get(node) ?? [])
        if (!distance.has(neighbour)) {
          distance.set(neighbour, step);
          next.push(neighbour);
        }
    frontier = next;
  }
  // A link from a node within depth relays reaches one step further, so its
  // other end was reached too; links between the outermost nodes are left out.
  const inside = (node: number) => (distance.get(node) ?? Infinity) <= depth;
  return {
    nodeIds: [...distance.keys()],
    linkKeys: links
      .filter(({ link }) => inside(link.node_a) || inside(link.node_b))
      .map(({ linkKind, link }) => linkKey(linkKind, link)),
  };
}

// Membership by match, which looks each value up instead of scanning the
// list as "in" does; match needs at least one label.
export function isOneOf(
  property: string,
  values: string[] | number[],
): ExpressionSpecification {
  return values.length === 0
    ? ["boolean", false]
    : ["match", ["get", property], values, true, false];
}

/**
 * Null shows every link. Otherwise only the given links show, more opaque
 * than usual, and the selected link fully opaque.
 */
export function showLinkFocus(
  map: Map,
  linkKind: LinkKind,
  linkKeys: string[] | null,
  selectedLinkKey: string | null,
): void {
  const ofNode: ExpressionSpecification[] =
    linkKeys === null ? [] : [isOneOf("linkKey", linkKeys)];
  const opacity = (base: number): ExpressionSpecification => [
    "case",
    ["==", ["get", "linkKey"], selectedLinkKey ?? ""],
    1,
    linkKeys === null ? base : Math.min(base + 0.3, 1),
  ];
  const layer = linkSourceId(linkKind);
  const noSnrLayer = noSnrLayerId(linkKind);
  map.setFilter(layer, ["all", ["!=", ["get", "tier"], "unknown"], ...ofNode]);
  map.setFilter(noSnrLayer, [
    "all",
    ["==", ["get", "tier"], "unknown"],
    ...ofNode,
  ]);
  map.setPaintProperty(layer, "line-opacity", opacity(LINK_OPACITY));
  map.setPaintProperty(noSnrLayer, "line-opacity", opacity(NO_SNR_OPACITY));
  if (linkKind === "traceroute" && map.getLayer(ZERO_COST_LAYER))
    map.setFilter(ZERO_COST_LAYER, ofNode[0] ?? null);
}

export type LinkLayerChoice = Record<LinkKind, boolean>;

// Direct receptions are many, so they start off, and so does neighbour
// info, which repeats much of what the other two show.
export const DEFAULT_LAYER_CHOICE: LinkLayerChoice = {
  traceroute: true,
  direct: false,
  neighbor: false,
};

// The "Observed at least" box starts here, to hide one-off sightings.
export const DEFAULT_MIN_OBSERVATIONS = "10";

// The length slider, a minimum, ends here: longer lines are rare and mostly
// left out by the server (links.map_misfit).
export const MAX_LINK_KM = 400;

/** The great-circle km between two drawn places. */
export function drawnKm(a: LatLon, b: LatLon): number {
  return distanceMeters(a, b) / 1000;
}

/**
 * Whether the map draws a link at least minKm long, between where it draws
 * both ends now, estimated positions included: the slider acts on the lines
 * as seen. A link with an end that is not drawn has no length, so any minimum
 * hides it.
 */
export function drawnLongEnough(
  link: Pick<MapLink, "node_a" | "node_b">,
  nodesById: ReadonlyMap<number, LatLon>,
  minKm: number,
): boolean {
  if (minKm <= 0) return true;
  const a = nodesById.get(link.node_a);
  const b = nodesById.get(link.node_b);
  return a !== undefined && b !== undefined && drawnKm(a, b) >= minKm;
}

/** Which quality tiers of every link kind are shown; see linkTierShown. */
export type LinkTierChoice = Record<Exclude<LinkTier, "unknown">, boolean>;

export const ALL_LINK_TIERS: LinkTierChoice = {
  good: true,
  fair: true,
  marginal: true,
};

/**
 * A link shows whole, both halves, when either half is of a chosen tier. A
 * half without an SNR does not count, so a link without an SNR either way has
 * no quality to filter on and always shows.
 */
export function linkTierShown(
  choice: LinkTierChoice,
  link: HalvedLink,
): boolean {
  const tiers = linkHalfTiers(link).filter((tier) => tier !== "unknown");
  return tiers.length === 0 || tiers.some((tier) => choice[tier]);
}
