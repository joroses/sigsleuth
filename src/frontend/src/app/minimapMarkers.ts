import type {
  Feature,
  FeatureCollection,
  LineString,
  Point,
  Position,
} from "geojson";

import type {
  NodeSummary,
  TracerouteHop,
  ReportPaths,
  RouteHopCost,
  RoutePath,
  TracerouteRun,
  PacketDetail,
} from "./api";
import { type LinkTier, linkTier, linkTierColor } from "./linkQuality";
import {
  formatNodeId,
  hopTone,
  hopsUsed,
  nodeLabel,
  nodeName,
} from "./nodeDisplay";

export interface MinimapMarker {
  key: string;
  /** sender is S: the packet's sender, or on a traceroute the node that ran it. */
  kind: "sender" | "target" | "gateway" | "relay";
  nodeId: number;
  name: string | null;
  /** "S", "R", the hops a gateway's reception used, empty for a relay. */
  label: string;
  tone: string;
  title: string;
  longitude: number;
  latitude: number;
  /** Drawn at an estimate from its neighbours, hollow: it reports no position. */
  estimated: boolean;
}

/** How far a line heard both ways sits to its own right, in pixels. */
export const TWO_WAY_OFFSET = 3;

export interface HopLineProperties extends DirectedHop {
  snr: number | null;
  tier: LinkTier;
  color: string;
  /** The reverse hop is drawn too, so this line is offset to its right. */
  twoWay: boolean;
  /** It spent no hop (zeroCostHops), so its arrow is the 0-cost blue. */
  zeroCost: boolean;
}

export function routeHopKey(
  from: number | null,
  to: number | null,
  isReturn: boolean,
): string {
  return `${from}-${to}:${isReturn}`;
}

/** The steps that spent none, kept apart on the outward and return paths. */
export function zeroCostHops(costs: RouteHopCost[]): Set<string> {
  return new Set(
    costs
      .filter((cost) => cost.spent === 0)
      .map((cost) =>
        routeHopKey(cost.from_node_id, cost.to_node_id, cost.is_return),
      ),
  );
}

/**
 * Where a hop's arrow and SNR label go, at the line's middle. rotation turns
 * an east-pointing arrow along the hop, clockwise in degrees. The label sits
 * on the line's right, where its own offset line is, anchored on the side
 * facing the line; labelOffset is in ems.
 */
export interface HopMarkProperties extends HopLineProperties {
  rotation: number;
  arrowOffset: [number, number];
  label: string | null;
  labelAnchor: string;
  labelOffset: [number, number];
}

export type HopFeatureCollection = FeatureCollection<
  LineString | Point,
  HopLineProperties | HopMarkProperties
>;

/** Traceroute hops as directed lines, one per hop with both nodes placed. */
export interface HopLines {
  features: HopFeatureCollection;
  hops: number;
  /** Hops left out because a node has no known position. */
  undrawnHops: number;
}

export interface MinimapContents {
  markers: MinimapMarker[];
  unplacedGateways: number;
  /**
   * Every hop the packet reported, and on a request its reply's route, plus
   * the listed routes' 0 dB steps (zeroDbSteps).
   */
  allHops: HopLines;
  /**
   * The hops of the routes each traceroute reception reported, by reception_id;
   * a request's reply route is under its reply reception's ID.
   */
  receptionHops: Map<number, HopLines>;
  /** Each relay's positions on each reception's routes, as marker labels. */
  receptionNumbers: Map<number, Map<number, string>>;
  /** The reports through each relay marker, by node ID. */
  relayReports: Map<number, RelayReports>;
  /** The reports with the hop minimapMarkers was given; null without one. */
  hopReports: RelayReports | null;
  /** Receptions that reported a route. */
  reports: number;
  /** Draw the SNR labels on allHops, as on a focused route. */
  labelAllHops?: boolean;
}

/**
 * The listed receptions whose routes pass through one relay, or cross one
 * hop, their hops and nodes.
 */
export interface RelayReports {
  receptionIds: Set<number>;
  hops: HopLines;
  nodeIds: Set<number>;
}

// A traceroute shows S where it started and R where it was going, and every
// other placed node in its routes as a relay; its gateways are not part of the
// route, so they get no marker. Other packets show the sender and each placed
// gateway, and count the gateways without a position. With a hop (from one
// node to another), it also finds the reports that crossed it that way.
export function minimapMarkers(
  detail: PacketDetail,
  hop: DirectedHop | null = null,
): MinimapContents {
  const markers =
    detail.traceroute === null
      ? packetMarkers(detail)
      : tracerouteMarkers(detail, detail.traceroute);
  const unplacedGateways =
    detail.traceroute === null
      ? detail.receptions.filter(
          (reception) =>
            !reception.own_upload &&
            (reception.gateway_latitude === null ||
              reception.gateway_longitude === null),
        ).length
      : 0;
  const positions = new Map(
    markers.map((marker): [number, [number, number]] => [
      marker.nodeId,
      [marker.longitude, marker.latitude],
    ]),
  );
  const free = zeroCostHops(detail.hop_costs);
  const hopLines = (hops: TracerouteHop[]) =>
    drawHops(detail.spreading_factor, hops, positions, free);
  const receptionHops = new Map<number, HopLines>();
  const receptionNumbers = new Map<number, Map<number, string>>();
  for (const reception of detail.receptions) {
    if (reception.paths === null) continue;
    receptionHops.set(
      reception.reception_id,
      hopLines(withZeroDbSteps(reception.hops, reception.paths)),
    );
    if (detail.traceroute !== null) {
      const { requester_node_id, target_node_id } = detail.traceroute;
      receptionNumbers.set(
        reception.reception_id,
        new Map(
          [
            ...routeNumbers(reception.paths, requester_node_id, target_node_id),
          ].map(([nodeId, numbers]) => [nodeId, numbers.join("·")]),
        ),
      );
    }
  }
  const reply = detail.reply_route;
  if (reply !== null && detail.traceroute !== null) {
    const { requester_node_id, target_node_id } = detail.traceroute;
    receptionHops.set(
      reply.reception_id,
      hopLines(
        withZeroDbSteps(reply.hops, { forward: reply.path, return_path: null }),
      ),
    );
    receptionNumbers.set(
      reply.reception_id,
      new Map(
        [
          ...routeNumbers(
            { forward: reply.path, return_path: null },
            requester_node_id,
            target_node_id,
          ),
        ].map(([nodeId, numbers]) => [nodeId, numbers.join("·")]),
      ),
    );
  }
  const drawn = ({ receptionIds, hops, nodeIds }: FoundReports) => ({
    receptionIds,
    hops: hopLines(hops),
    nodeIds,
  });
  const relayReports = new Map<number, RelayReports>();
  for (const marker of markers) {
    if (marker.kind !== "relay") continue;
    relayReports.set(
      marker.nodeId,
      drawn(reportsThrough(detail, marker.nodeId)),
    );
  }
  return {
    markers,
    unplacedGateways,
    allHops: hopLines([
      ...detail.hops,
      ...(reply?.hops ?? []),
      ...listedReports(detail).flatMap((report) => report.zeroDbSteps),
    ]),
    receptionHops,
    receptionNumbers,
    relayReports,
    hopReports:
      hop === null
        ? null
        : drawn(reportsWithHop(detail, hop.fromNodeId, hop.toNodeId)),
    reports: receptionHops.size,
  };
}

/**
 * One traceroute run's longest route on its own: S at the requester and R at
 * the target whichever way the route went, its placed relays numbered along it from
 * 1 (a gap takes a number), and one line per hop or 0 dB step with its SNR
 * label. Nodes sit where they were when the route was heard (run.positions),
 * else where the page's nodes place them now.
 */
export function routeMinimap(
  run: TracerouteRun,
  nodes: ReadonlyMap<number, NodeSummary>,
): MinimapContents {
  const { path } = run.route;
  const numbers = new Map<number, number[]>();
  path.nodes.forEach((nodeId, position) => {
    if (
      nodeId === null ||
      nodeId === run.requester_node_id ||
      nodeId === run.target_node_id
    ) {
      return;
    }
    numbers.set(nodeId, [...(numbers.get(nodeId) ?? []), position]);
  });
  const markers: MinimapMarker[] = [];
  const heard = new Map(run.positions.map((at) => [at.node_id, at]));
  const place = (
    nodeId: number,
    kind: MinimapMarker["kind"],
    label: string,
    role: string,
  ) => {
    const now = nodes.get(nodeId);
    if (now === undefined) return;
    const then = heard.get(nodeId);
    const node =
      then === undefined
        ? now
        : {
            ...now,
            latitude: then.latitude,
            longitude: then.longitude,
            estimated: false,
          };
    if (node.latitude === null || node.longitude === null) return;
    markers.push({
      key: kind === "relay" ? `relay-${nodeId}` : kind,
      kind,
      nodeId,
      name: nodeName(node),
      label,
      tone: kind === "relay" ? "hop-tone-relay" : "hop-tone-sender",
      title: withEstimate(
        `${role}: ${nodeName(node) ?? formatNodeId(nodeId)}`,
        node.estimated,
      ),
      longitude: node.longitude,
      latitude: node.latitude,
      estimated: node.estimated,
    });
  };
  place(run.requester_node_id, "sender", "S", "Traceroute from");
  place(run.target_node_id, "target", "R", "Traceroute to");
  for (const [nodeId, positions] of numbers) {
    place(
      nodeId,
      "relay",
      positions.join("·"),
      `Relay ${positions.join(", ")}`,
    );
  }
  const positions = new Map(
    markers.map((marker): [number, [number, number]] => [
      marker.nodeId,
      [marker.longitude, marker.latitude],
    ]),
  );
  return {
    markers,
    unplacedGateways: 0,
    allHops: drawHops(
      run.spreading_factor,
      withZeroDbSteps(
        run.route.path_hops.map((hop) => ({
          ...hop,
          is_return: run.route.is_return,
        })),
        {
          forward: run.route.path,
          return_path: null,
        },
      ),
      positions,
      zeroCostHops(run.route.hop_costs),
    ),
    receptionHops: new Map(),
    receptionNumbers: new Map(),
    relayReports: new Map(),
    hopReports: null,
    reports: 1,
    labelAllHops: true,
  };
}

/** from_node_id transmitted and to_node_id heard it. */
export interface DirectedHop {
  fromNodeId: number;
  toNodeId: number;
}

export function sameHop(
  a: DirectedHop | null | undefined,
  b: DirectedHop | null | undefined,
): boolean {
  return (
    a != null &&
    b != null &&
    a.fromNodeId === b.fromNodeId &&
    a.toNodeId === b.toNodeId
  );
}

/** Reports found by reportsThrough or reportsWithHop, before they are drawn. */
interface FoundReports {
  receptionIds: Set<number>;
  hops: TracerouteHop[];
  nodeIds: Set<number>;
}

/**
 * The receptions whose forward or return route includes the node (and on a
 * request its reply's route, under the reply reception's ID), the hops of
 * their routes, each hop once, and every node on them.
 */
export function reportsThrough(
  detail: PacketDetail,
  nodeId: number,
): FoundReports {
  return foundReports(detail, (report) => report.nodes.includes(nodeId));
}

/**
 * As reportsThrough, for the reports with a measured hop from one node to the
 * other, in that direction.
 */
export function reportsWithHop(
  detail: PacketDetail,
  fromNodeId: number,
  toNodeId: number,
): FoundReports {
  return foundReports(detail, (report) =>
    report.hops.some(
      (hop) => hop.from_node_id === fromNodeId && hop.to_node_id === toNodeId,
    ),
  );
}

interface ListedReport {
  receptionId: number;
  paths: ReportPaths;
  nodes: (number | null)[];
  /** Its measured hops, as the API sends them. */
  hops: TracerouteHop[];
  /** Its steps at exactly 0 dB (zeroDbSteps), drawn with its hops. */
  zeroDbSteps: TracerouteHop[];
}

// Each listed reception's forward and return routes, and a request's reply route.
function listedReports(detail: PacketDetail): ListedReport[] {
  const reports: ListedReport[] = [];
  for (const reception of detail.receptions) {
    const { paths } = reception;
    if (paths === null) continue;
    reports.push({
      receptionId: reception.reception_id,
      paths,
      nodes: [...paths.forward.nodes, ...(paths.return_path?.nodes ?? [])],
      hops: reception.hops,
      zeroDbSteps: zeroDbSteps(paths),
    });
  }
  const reply = detail.reply_route;
  if (reply !== null) {
    const paths = { forward: reply.path, return_path: null };
    reports.push({
      receptionId: reply.reception_id,
      paths,
      nodes: reply.path.nodes,
      hops: reply.hops,
      zeroDbSteps: zeroDbSteps(paths),
    });
  }
  return reports;
}

function foundReports(
  detail: PacketDetail,
  keep: (report: ListedReport) => boolean,
): FoundReports {
  const receptionIds = new Set<number>();
  const hops = new Map<string, TracerouteHop>();
  const nodeIds = new Set<number>();
  for (const report of listedReports(detail)) {
    if (!keep(report)) continue;
    receptionIds.add(report.receptionId);
    for (const node of report.nodes) if (node !== null) nodeIds.add(node);
    for (const hop of report.hops) {
      hops.set(`${hop.from_node_id}-${hop.to_node_id}`, hop);
    }
    // A measured hop between the same nodes is drawn instead.
    for (const step of report.zeroDbSteps) {
      const key = `${step.from_node_id}-${step.to_node_id}`;
      if (!hops.has(key)) hops.set(key, step);
    }
  }
  return { receptionIds, hops: [...hops.values()], nodeIds };
}

/**
 * The routes' steps at exactly 0 dB between two known, different nodes. The
 * API's hops leave them out, as they may be MQTT hops (MQTT_HOP_SNR_DB in
 * traceroute.py), so they never count toward km or links; the minimaps still
 * draw them, as the route text lists them.
 */
export function zeroDbSteps(paths: ReportPaths): TracerouteHop[] {
  return [paths.forward, paths.return_path].flatMap((path, direction) =>
    path === null
      ? []
      : path.snr_db.flatMap((snr, index) => {
          const from = path.nodes[index];
          const to = path.nodes[index + 1];
          return snr === 0 && from !== null && to !== null && from !== to
            ? [
                {
                  from_node_id: from,
                  to_node_id: to,
                  snr_db: snr,
                  is_return: direction === 1,
                },
              ]
            : [];
        }),
  );
}

function withZeroDbSteps(
  hops: TracerouteHop[],
  paths: ReportPaths | null,
): TracerouteHop[] {
  return paths === null ? hops : [...hops, ...zeroDbSteps(paths)];
}

function withEstimate(title: string, estimated: boolean): string {
  return estimated ? `${title} (estimated position)` : title;
}

function packetMarkers(detail: PacketDetail): MinimapMarker[] {
  const { packet } = detail;
  const markers: MinimapMarker[] = [];
  if (detail.sender_latitude !== null && detail.sender_longitude !== null) {
    markers.push({
      key: "sender",
      kind: "sender",
      nodeId: packet.sender_node_id,
      name: packet.sender_name,
      label: "S",
      tone: "hop-tone-sender",
      title: withEstimate(
        `Sender: ${packet.sender_name ?? formatNodeId(packet.sender_node_id)}`,
        detail.sender_estimated,
      ),
      longitude: detail.sender_longitude,
      latitude: detail.sender_latitude,
      estimated: detail.sender_estimated,
    });
  }
  for (const reception of detail.receptions) {
    // The sender's own upload would sit on the sender's mark.
    if (
      reception.own_upload ||
      reception.gateway_latitude === null ||
      reception.gateway_longitude === null
    ) {
      continue;
    }
    const hops = hopsUsed(reception);
    markers.push({
      key: `reception-${reception.reception_id}`,
      kind: "gateway",
      nodeId: reception.gateway_node_id,
      name: reception.gateway_name,
      label: hops,
      tone: hopTone(reception),
      title: withEstimate(
        `${reception.gateway_name ?? formatNodeId(reception.gateway_node_id)}: ${
          hops === "?" ? "hops not reported" : `${hops} hops used`
        }`,
        reception.gateway_estimated,
      ),
      longitude: reception.gateway_longitude,
      latitude: reception.gateway_latitude,
      estimated: reception.gateway_estimated,
    });
  }
  return markers;
}

function tracerouteMarkers(
  detail: PacketDetail,
  {
    requester_node_id,
    target_node_id,
  }: NonNullable<PacketDetail["traceroute"]>,
): MinimapMarker[] {
  const markers: MinimapMarker[] = [];
  const ends: [number, MinimapMarker["kind"], string, string][] = [
    [requester_node_id, "sender", "S", "Traceroute from"],
    [target_node_id, "target", "R", "Traceroute to"],
  ];
  for (const node of detail.route_nodes) {
    if (node.latitude === null || node.longitude === null) continue;
    const end = ends.find(([nodeId]) => nodeId === node.node_id);
    const name = nodeLabel(node);
    markers.push({
      key: end === undefined ? `relay-${node.node_id}` : end[1],
      kind: end?.[1] ?? "relay",
      nodeId: node.node_id,
      name: nodeName(node),
      label: end?.[2] ?? "",
      tone: end === undefined ? "hop-tone-relay" : "hop-tone-sender",
      title: withEstimate(`${end?.[3] ?? "Relay"}: ${name}`, node.estimated),
      longitude: node.longitude,
      latitude: node.latitude,
      estimated: node.estimated,
    });
  }
  // S and R first, so they are drawn above the relays.
  const rank = { sender: 0, target: 1, gateway: 2, relay: 2 };
  return markers.sort((a, b) => rank[a.kind] - rank[b.kind]);
}

/**
 * Each relay's positions on one reception's routes, counting from the
 * requester: the first relay is 1. A reply counts on through the return route,
 * so a relay on both routes has two positions. A gap takes a position; S and R get none.
 */
export function routeNumbers(
  paths: ReportPaths,
  requester: number,
  target: number,
): Map<number, number[]> {
  const numbers = new Map<number, number[]>();
  const nodes = [
    ...paths.forward.nodes,
    ...(paths.return_path?.nodes.slice(1) ?? []),
  ];
  nodes.forEach((nodeId, position) => {
    if (nodeId === null || nodeId === requester || nodeId === target) return;
    numbers.set(nodeId, [...(numbers.get(nodeId) ?? []), position]);
  });
  return numbers;
}

// One line per hop from the node that sent to the node that heard, each hop
// once, plus a point at its middle for the arrow and the SNR label. A 0 dB
// step is no measured hop, so it never counts as 0-cost.
function drawHops(
  spreadingFactor: number | null,
  hops: TracerouteHop[],
  positions: ReadonlyMap<number, [number, number]>,
  zeroCost: ReadonlySet<string>,
): HopLines {
  const drawn = new Map<string, TracerouteHop>();
  let undrawnHops = 0;
  for (const hop of hops) {
    if (
      !positions.has(hop.from_node_id) ||
      !positions.has(hop.to_node_id) ||
      hop.from_node_id === hop.to_node_id
    ) {
      undrawnHops += 1;
      continue;
    }
    const key = `${hop.from_node_id}-${hop.to_node_id}`;
    if (!drawn.has(key)) drawn.set(key, hop);
  }
  const features: Feature<
    LineString | Point,
    HopLineProperties | HopMarkProperties
  >[] = [];
  for (const hop of drawn.values()) {
    const start = positions.get(hop.from_node_id)!;
    const end = positions.get(hop.to_node_id)!;
    const tier = linkTier(hop.snr_db, spreadingFactor);
    const line: HopLineProperties = {
      fromNodeId: hop.from_node_id,
      toNodeId: hop.to_node_id,
      snr: hop.snr_db,
      tier,
      color: linkTierColor(tier),
      twoWay: drawn.has(`${hop.to_node_id}-${hop.from_node_id}`),
      zeroCost:
        hop.snr_db !== 0 &&
        zeroCost.has(
          routeHopKey(hop.from_node_id, hop.to_node_id, hop.is_return),
        ),
    };
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [start, end] },
      properties: line,
    });
    features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: mercatorMidpoint(start, end) },
      properties: { ...line, ...hopMark(start, end, line) },
    });
  }
  return {
    features: { type: "FeatureCollection", features },
    hops: hops.length,
    undrawnHops,
  };
}

// Label anchors by the direction from the line to the label, clockwise from
// east in screen terms: a label to the line's east is anchored on its left.
const LABEL_ANCHORS = [
  "left",
  "top-left",
  "top",
  "top-right",
  "right",
  "bottom-right",
  "bottom",
  "bottom-left",
];

/**
 * How far to turn an east-pointing arrow so it points from start to end on
 * screen, clockwise in degrees, on a minimap never rotated or tilted.
 */
export function arrowRotation(
  start: [number, number],
  end: [number, number],
): number {
  const east = ((end[0] - start[0]) * Math.PI) / 180;
  const south = mercatorY(start[1]) - mercatorY(end[1]);
  return (Math.atan2(south, east) * 180) / Math.PI;
}

// Screen directions assume the minimap is never rotated or tilted.
function hopMark(
  start: [number, number],
  end: [number, number],
  line: HopLineProperties,
): Omit<HopMarkProperties, keyof HopLineProperties> {
  const east = ((end[0] - start[0]) * Math.PI) / 180;
  const south = mercatorY(start[1]) - mercatorY(end[1]);
  const length = Math.hypot(east, south) || 1;
  // The unit vector to the line's right on screen, where y points down.
  const right: [number, number] = [-south / length, east / length];
  const rightAngle = Math.atan2(right[1], right[0]);
  const octant = (Math.round(rightAngle / (Math.PI / 4)) + 8) % 8;
  const labelDistance = line.twoWay ? 0.9 : 0.6;
  return {
    rotation: arrowRotation(start, end),
    arrowOffset: [0, line.twoWay ? TWO_WAY_OFFSET : 0],
    label: line.snr === null ? null : `${line.snr} dB`,
    labelAnchor: LABEL_ANCHORS[octant],
    labelOffset: [right[0] * labelDistance, right[1] * labelDistance],
  };
}

function mercatorY(latitude: number): number {
  return Math.log(Math.tan(Math.PI / 4 + (latitude * Math.PI) / 360));
}

/**
 * The middle of a line as the map draws it, straight in Web Mercator, so an
 * arrow sits on the line. Crossing the antimeridian is not handled.
 */
export function mercatorMidpoint(
  start: Position,
  end: Position,
): [number, number] {
  const y = (mercatorY(start[1]) + mercatorY(end[1])) / 2;
  const latitude = (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * (180 / Math.PI);
  return [(start[0] + end[0]) / 2, latitude];
}

/** One report of a traceroute drawn on the main map, with its nodes. */
export interface TracerouteDrawing {
  /** The report's routes; null when no gateway reported one. */
  paths: ReportPaths | null;
  features: HopFeatureCollection;
  /** S, R and each placed relay numbered along the route, as on the packet page. */
  markers: MinimapMarker[];
  /** The route's nodes, which stay on the map whatever the filters say. */
  nodeIds: number[];
}

/**
 * The route of one traceroute packet that crossed hop: of the gateways'
 * reports, the saved report that also serves the explorer's hop search.
 */
export function tracerouteDrawing(
  detail: PacketDetail,
  hop: DirectedHop,
): TracerouteDrawing {
  const { markers, receptionHops, receptionNumbers } = minimapMarkers(detail);
  const reports = listedReports(detail);
  const chosen = detail.hop_routes.find(
    (route) =>
      route.from_node_id === hop.fromNodeId &&
      route.to_node_id === hop.toNodeId,
  );
  const best =
    reports.find((report) => report.receptionId === chosen?.reception_id) ??
    null;
  const ends = detail.traceroute;
  const nodeIds = new Set<number>();
  if (ends !== null)
    nodeIds.add(ends.requester_node_id).add(ends.target_node_id);
  for (const nodeId of best?.nodes ?? [])
    if (nodeId !== null) nodeIds.add(nodeId);
  const numbers =
    best === null ? undefined : receptionNumbers.get(best.receptionId);
  return {
    paths: best?.paths ?? null,
    features:
      best === null
        ? { type: "FeatureCollection", features: [] }
        : receptionHops.get(best.receptionId)!.features,
    markers: markers
      .filter((marker) => nodeIds.has(marker.nodeId))
      .map((marker) =>
        marker.kind === "relay"
          ? { ...marker, label: numbers?.get(marker.nodeId) ?? "" }
          : marker,
      ),
    nodeIds: [...nodeIds],
  };
}

/** One line of the route list. */
export type RouteRow =
  | {
      kind: "node";
      /** null: a relay that did not record itself. */
      nodeId: number | null;
      /** S, R or its place along the route, as on its map marker. */
      label: string;
      /** The hop that reached it, null for the first node. */
      hop: DirectedHop | null;
      /** undefined for the first node; null when no SNR was recorded. */
      snr?: number | null;
      /** Where the route was going but was not heard to get. */
      unreached?: boolean;
    }
  | { kind: "gap"; text: string }
  | { kind: "back" };

/**
 * The report's route node by node: S first, then each node with the SNR it
 * heard the one before at, numbered as on the map, to R (and back to S on a
 * reply). A route that does not start at S, or stops short of where it was
 * going, gets a gap row there.
 */
export function routeRows(
  paths: ReportPaths,
  requester: number,
  target: number,
): RouteRow[] {
  const rows: RouteRow[] = [];
  const label = (nodeId: number | null, place: number) =>
    nodeId === requester ? "S" : nodeId === target ? "R" : String(place);
  const add = (
    { nodes, snr_db: snrs, reached }: RoutePath,
    first: number,
    start: number,
    end: number,
  ) => {
    let unreported = false;
    if (first === 0 && nodes[0] !== start) {
      rows.push({ kind: "node", nodeId: start, label: "S", hop: null });
      rows.push({ kind: "gap", text: "Not reported" });
    }
    nodes.forEach((nodeId, index) => {
      // A return route starts where the way out ended.
      if (index === 0 && first > 0) return;
      const before = index === 0 ? null : nodes[index - 1];
      if (index > 0 && index - 1 >= snrs.length && !unreported) {
        unreported = true;
        rows.push({
          kind: "gap",
          text: reached ? "Not reported" : "Not confirmed",
        });
      }
      rows.push({
        kind: "node",
        nodeId,
        label: label(nodeId, first + index),
        hop:
          before === null || nodeId === null || unreported
            ? null
            : { fromNodeId: before, toNodeId: nodeId },
        ...(index === 0
          ? {}
          : unreported
            ? { unreached: !reached }
            : { snr: snrs[index - 1] }),
      });
    });
    if (nodes.at(-1) !== end) {
      if (!unreported)
        rows.push({
          kind: "gap",
          text: reached ? "Not reported" : "Not confirmed",
        });
      rows.push({
        kind: "node",
        nodeId: end,
        label: label(end, 0),
        hop: null,
        unreached: !reached,
      });
    }
  };
  const { forward, return_path: back } = paths;
  add(forward, 0, requester, target);
  if (back !== null) {
    rows.push({ kind: "back" });
    add(back, forward.nodes.length - 1, target, requester);
  }
  return rows;
}
