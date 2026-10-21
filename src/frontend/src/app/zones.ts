// Presentation helpers for the Zones page: zone colours, shaded areas, relay
// marks, and the backend's key relays (zones.py decides them) looked up for
// drawing.

import type {
  NodeSummary,
  Zone,
  KeyRelay,
  NetworkZones,
  ZonePair,
  ZonePairLink,
  ZoneRules,
  ZoneTextReach,
} from "./api";

/**
 * The chart series colours in styles.css, validated for colour blindness,
 * then four more so a busy channel's biggest zones each get their own.
 */
export const ZONE_COLORS = [
  "#3987e5",
  "#d95926",
  "#199e70",
  "#c98500",
  "#d55181",
  "#008300",
  "#9085e9",
  "#e66767",
  "#17a2c4",
  "#8b5e34",
  "#7f8a00",
  "#6a3d9a",
];

/** The colour of a zone outside the biggest ZONE_COLORS.length. */
export const ZONE_GREY = "#6b7570";

/**
 * A colour per zone: the biggest zones (most members, then lowest number)
 * each get their own while the palette lasts, so a zone's swatch in the
 * tables matches one area on the map; the rest are grey.
 */
export function zoneColors(zones: Zone[]): Map<number, string> {
  const ranked = [...zones].sort(
    (a, b) => b.members.length - a.members.length || a.zone_id - b.zone_id,
  );
  return new Map(
    ranked.map((zone, index) => [
      zone.zone_id,
      ZONE_COLORS[index] ?? ZONE_GREY,
    ]),
  );
}

/** The nodes drawn at an estimate: they report no position. */
export function estimatedNodes(network: NetworkZones): Set<number> {
  return new Set(
    network.nodes.filter((node) => node.estimated).map((node) => node.node_id),
  );
}

/** A zone's shown name: the town or city it is named after, else its hub's name. */
export function zoneName(zone: Zone): string {
  return zone.place ?? zone.name;
}

/**
 * The colour of a key relay's mast on the map, and beside its name in
 * tables: the mast itself, or the disc behind it when the relay carries an
 * only solid link.
 */
export const RELAY_COLOR = "#d0453a";
/**
 * A key relay's mark, as SVG path data for the map's image and the tables'
 * icon: a lattice mast with two radio waves each side, about 25 units
 * across and centred on (0, 0). RELAY_MAST is stroked; RELAY_MAST_TIP, the
 * aerial's ball, is filled.
 */
export const RELAY_MAST = [
  "M-5 11 L0 -3 L5 11 M-2.5 4 H2.5 M-2.5 4 L3.9 8 M2.5 4 L-3.9 8",
  "M3.66 -8.41 A5 5 0 0 1 3.66 -1.59 M-3.66 -1.59 A5 5 0 0 1 -3.66 -8.41",
  "M6.22 -10.79 A8.5 8.5 0 0 1 6.22 0.79 M-6.22 0.79 A8.5 8.5 0 0 1 -6.22 -10.79",
].join(" ");
export const RELAY_MAST_TIP =
  "M-1.8 -5 A1.8 1.8 0 1 0 1.8 -5 A1.8 1.8 0 1 0 -1.8 -5 Z";
/**
 * Whether a key relay leaves enough nodes cut off or without a solid backup
 * (KEY_RELAY_CUT in zones.py) to be marked with a bare mast.
 */
export function isOutlined(relay: KeyRelay, rules: ZoneRules): boolean {
  return relay.impact >= rules.key_relay_cut;
}

/**
 * Whether a key relay carries the only solid link between its zone and
 * another. It is marked above the others: a white mast on a disc of
 * RELAY_COLOR.
 */
export function holdsOnlySolidLink(relay: KeyRelay): boolean {
  return relay.only_solid_link_to.length > 0;
}

// A key relay not seen for longer is shown as late.
export const LATE_HOURS = 3;
// A battery level at or below this percentage is shown as low.
export const LOW_BATTERY = 20;
const HOUR_MS = 60 * 60 * 1000;
/** The colour of a key relay's warning mark on the map. */
export const WARNING_COLOR = "#f2b01e";

/**
 * Why a key relay may need a visit: not heard for a while, or a low battery;
 * null when neither. `now` is when the page's data was made.
 */
export function relayConcern(relay: KeyRelay, now: number): string | null {
  const hours = hoursSince(relay.last_seen_at, now);
  if (hours !== null && hours > LATE_HOURS) {
    return `Last heard ${formatHours(hours)} ago`;
  }
  if (relay.battery_level !== null && relay.battery_level <= LOW_BATTERY) {
    return `Battery at ${relay.battery_level}%`;
  }
  return null;
}

/**
 * How the map marks a key relay: a mast on a disc for an only solid link, a
 * bare mast when other nodes depend on it, or, for any other key relay with
 * a warning (relayConcern), the warning mark alone; null leaves it off.
 */
export function relayMark(
  relay: KeyRelay,
  now: number,
  rules: ZoneRules,
): "filled" | "outlined" | "warning" | null {
  if (holdsOnlySolidLink(relay)) return "filled";
  if (isOutlined(relay, rules)) return "outlined";
  return relayConcern(relay, now) !== null ? "warning" : null;
}

export function hoursSince(value: string | null, now: number): number | null {
  return value === null ? null : (now - new Date(value).getTime()) / HOUR_MS;
}

export function formatHours(hours: number): string {
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min`;
  if (hours < 48)
    return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} h`;
  return `${Math.round(hours / 24)} days`;
}

/** What a key relay matters for, in a few words, such as "cuts off 4". */
export function relayReason(
  relay: KeyRelay,
  names: Map<number, string>,
  rules: ZoneRules,
): string {
  if (holdsOnlySolidLink(relay)) {
    const zones = relay.only_solid_link_to.map((id) => names.get(id) ?? id);
    return `only solid link to ${zones.join(", ")}`;
  }
  if (relay.cuts_off >= rules.key_relay_cut) {
    return `cuts off ${relay.cuts_off}`;
  }
  if (relay.no_solid_backup >= rules.key_relay_cut) {
    return `no solid backup for ${relay.no_solid_backup}`;
  }
  return `carries ${relay.carries}`;
}

/** A zone pair's key in selections and on the map. */
export function pairKey(pair: Pick<ZonePair, "zone_a" | "zone_b">): string {
  return `${pair.zone_a}-${pair.zone_b}`;
}

/**
 * What the page says about texts from one zone reaching another: the figure,
 * "no gateway" when the receiving zone has no gateway that reported a text,
 * else null (no sender in the first zone had enough texts heard there).
 */
export type TextReachFigure = ZoneTextReach | "no gateway" | null;

/** Looks up text reach from one zone to another. */
export function textReachLookup(
  network: Pick<NetworkZones, "zones" | "text_reach">,
): (fromZone: number, toZone: number) => TextReachFigure {
  const reach = new Map(
    network.text_reach.map((item) => [
      `${item.from_zone}-${item.to_zone}`,
      item,
    ]),
  );
  const silent = new Set(
    network.zones
      .filter((zone) => zone.text_gateways === 0)
      .map((zone) => zone.zone_id),
  );
  return (fromZone, toZone) =>
    reach.get(`${fromZone}-${toZone}`) ??
    (silent.has(toZone) ? "no gateway" : null);
}

/** A share between 0 and 1 as a whole percentage, such as "94%". */
export function formatShare(share: number): string {
  return `${Math.round(share * 100)}%`;
}

type Point = [number, number];

/** A node's position as [longitude, latitude], or null. */
export function placed(node: NodeSummary | undefined): Point | null {
  return node?.latitude == null || node.longitude == null
    ? null
    : [node.longitude, node.latitude];
}

/** The links the map can draw: those with both nodes placed. */
export function drawnLinks(
  links: readonly ZonePairLink[],
  nodes: Map<number, NodeSummary>,
): { link: ZonePairLink; a: Point; b: Point }[] {
  return links.flatMap((link) => {
    const a = placed(nodes.get(link.node_a));
    const b = placed(nodes.get(link.node_b));
    return a === null || b === null ? [] : [{ link, a, b }];
  });
}

/**
 * The links on a zone's shortest solid paths to the other zones (solid_paths
 * in zones.py), which the map draws while the zone is selected.
 */
export function solidPathLinks(
  network: Pick<NetworkZones, "zones" | "path_links">,
  zoneId: number | null,
): ZonePairLink[] {
  const zone = network.zones.find((item) => item.zone_id === zoneId);
  const keys = new Set(zone?.solid_paths.map(([a, b]) => `${a}-${b}`));
  return network.path_links.filter((link) =>
    keys.has(`${link.node_a}-${link.node_b}`),
  );
}

/**
 * The link a zone line is drawn along, between the nodes that join the two
 * zones: of the links the map can draw, the solid ones if any,
 * the one with the best weaker margin, then the most observed. Null when no
 * link has both nodes placed.
 */
export function mostSolidLink(
  pair: ZonePair,
  nodes: Map<number, NodeSummary>,
): { link: ZonePairLink; a: Point; b: Point } | null {
  const drawn = drawnLinks(pair.links, nodes);
  const solid = drawn.filter((item) => item.link.solid);
  let best: (typeof drawn)[number] | null = null;
  for (const item of solid.length > 0 ? solid : drawn) {
    if (
      best === null ||
      item.link.weaker_margin_db > best.link.weaker_margin_db ||
      (item.link.weaker_margin_db === best.link.weaker_margin_db &&
        item.link.observations > best.link.observations)
    ) {
      best = item;
    }
  }
  return best;
}

/**
 * The shaded area of a zone: the convex hull of its placed members as
 * [longitude, latitude], leaving out lone members (a far-off or stale
 * position would stretch the area across the map). Fewer than 3 points make
 * no area.
 */
export function zoneHull(points: Point[]): Point[] {
  return convexHull(withoutLoneMembers(points));
}

// A member with no other member this close is left out of its zone's area.
const LONE_MEMBER_KM = 50;
const KM_PER_DEGREE = 111.2;

/**
 * Drops points with no other point within LONE_MEMBER_KM, on a flat map
 * whose longitudes are scaled by the cosine of the latitude.
 */
export function withoutLoneMembers(points: Point[]): Point[] {
  const km = (a: Point, b: Point) =>
    KM_PER_DEGREE *
    Math.hypot(
      (a[0] - b[0]) * Math.cos(((a[1] + b[1]) * Math.PI) / 360),
      a[1] - b[1],
    );
  return points.filter((point, index) =>
    points.some(
      (other, otherIndex) =>
        otherIndex !== index && km(point, other) <= LONE_MEMBER_KM,
    ),
  );
}

/** Andrew's monotone chain, counter-clockwise; under 3 points are returned as they are. */
export function convexHull(points: Point[]): Point[] {
  const sorted = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (sorted.length < 3) return sorted;
  const cross = (o: Point, a: Point, b: Point) =>
    (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = (ordered: Point[]) => {
    const chain: Point[] = [];
    for (const point of ordered) {
      while (
        chain.length >= 2 &&
        cross(chain[chain.length - 2], chain[chain.length - 1], point) <= 0
      ) {
        chain.pop();
      }
      chain.push(point);
    }
    return chain.slice(0, -1);
  };
  return [...half(sorted), ...half([...sorted].reverse())];
}

/**
 * The centre by area of a convex hull, which is always inside it, on a flat
 * map of [longitude, latitude]. A hull with no area (all points on a line)
 * gives the mean of its points; no points give null.
 */
export function hullCentre(hull: Point[]): Point | null {
  if (hull.length === 0) return null;
  let twiceArea = 0;
  let x = 0;
  let y = 0;
  hull.forEach((a, index) => {
    const b = hull[(index + 1) % hull.length];
    const cross = a[0] * b[1] - b[0] * a[1];
    twiceArea += cross;
    x += (a[0] + b[0]) * cross;
    y += (a[1] + b[1]) * cross;
  });
  if (Math.abs(twiceArea) < 1e-12) {
    const mean = (axis: 0 | 1) =>
      hull.reduce((sum, point) => sum + point[axis], 0) / hull.length;
    return [mean(0), mean(1)];
  }
  return [x / (3 * twiceArea), y / (3 * twiceArea)];
}

/** What looks weak about a zone pair (ZonePair.warnings in zones.py). */
export type PairWarning = ZonePair["warnings"][number];

/**
 * Whether two zones have heard links directly between them. Pairs without
 * are zones joined only through other zones; the map draws no
 * line for them.
 */
export function isDirect(pair: ZonePair): boolean {
  return pair.links.length > 0;
}

/** Looks up text reach from one zone to another (textReachLookup). */
export type ReachLookup = (fromZone: number, toZone: number) => TextReachFigure;

/**
 * The lower text share of the pair's two directions, leaving out a direction
 * with no figure; null with neither.
 */
export function worseTextShare(
  pair: ZonePair,
  reach: ReachLookup,
): number | null {
  const shares = [
    reach(pair.zone_a, pair.zone_b),
    reach(pair.zone_b, pair.zone_a),
  ].flatMap((figure) =>
    figure === null || figure === "no gateway" ? [] : [figure.share],
  );
  return shares.length === 0 ? null : Math.min(...shares);
}
