import type { Feature, FeatureCollection, LineString, Point } from "geojson";

import type { FloodRoutes, PacketDetail, PacketReception } from "./api";
import type { MinimapFocus } from "./PacketMinimap";
import { linkTier, linkTierColor } from "./linkQuality";
import { zeroCostColor } from "./mapLinkLayers";
import { arrowRotation, mercatorMidpoint } from "./minimapMarkers";

interface RelayFeatureProperties {
  kind: "route" | "route-arrow" | "relay";
  certainty: "known" | "guess";
  estimated: boolean;
  color: string;
  rotation: number;
  label: string | null;
  nodeId: number;
  active: boolean;
  selected: boolean;
  reception: boolean;
}

const ROUTE_COLOR = "#64748b";

export type PacketRelays = FeatureCollection<
  LineString | Point,
  RelayFeatureProperties
>;

/** Relays, likely connections and the selected gateway reports. */
export function packetRelays(
  detail: PacketDetail,
  routes: FloodRoutes | null = null,
  focus: MinimapFocus = null,
): PacketRelays | null {
  if (
    detail.traceroute !== null ||
    (focus === null &&
      !detail.receptions.some(
        (reception) => !reception.own_upload && (hopsOf(reception) ?? 0) > 0,
      ))
  ) {
    return null;
  }
  const sender = detail.packet.sender_node_id;
  const nodes = new Map(
    [
      ...detail.relay_nodes,
      ...(routes?.nodes ?? []),
      ...detail.receptions
        .filter((reception) => !reception.own_upload)
        .map((reception) => ({
          node_id: reception.gateway_node_id,
          short_name: reception.gateway_short_name,
          long_name: reception.gateway_name,
          latitude: reception.gateway_latitude,
          longitude: reception.gateway_longitude,
          estimated: reception.gateway_estimated,
        })),
    ].map((node) => [node.node_id, node]),
  );
  const positions = new Map<number, [number, number]>();
  for (const node of nodes.values()) {
    if (node.latitude !== null && node.longitude !== null) {
      positions.set(node.node_id, [node.longitude, node.latitude]);
    }
  }
  if (detail.sender_latitude !== null && detail.sender_longitude !== null) {
    positions.set(sender, [detail.sender_longitude, detail.sender_latitude]);
  }

  const selectedReports = detail.receptions.filter((report) =>
    focus?.kind === "reception"
      ? report.reception_id === focus.receptionId
      : focus?.kind === "relay"
        ? report.relay_node?.node_id === focus.nodeId
        : false,
  );
  const activeNodes = new Set<number>();
  if (focus?.kind === "relay") activeNodes.add(focus.nodeId);
  for (const report of focus?.kind === "relay" ? [] : selectedReports) {
    if (report.direct) activeNodes.add(sender);
    else if (report.relay_node !== null)
      activeNodes.add(report.relay_node.node_id);
  }
  let changed = true;
  while (changed) {
    changed = false;
    for (const hop of routes?.hops ?? []) {
      if (
        activeNodes.has(hop.to_node_id) ||
        (focus?.kind === "relay" && activeNodes.has(hop.from_node_id))
      ) {
        for (const nodeId of [hop.from_node_id, hop.to_node_id]) {
          if (!activeNodes.has(nodeId)) {
            activeNodes.add(nodeId);
            changed = true;
          }
        }
      }
    }
  }
  const relays = new Set<number>();
  for (const reception of detail.receptions) {
    if (reception.own_upload || hopsOf(reception) === null) continue;
    if (reception.relay_node !== null) {
      relays.add(reception.relay_node.node_id);
    } else {
      for (const maybe of reception.relay_maybe) relays.add(maybe.node_id);
    }
  }
  const features: Feature<LineString | Point, RelayFeatureProperties>[] = [];
  for (const hop of routes?.hops ?? []) {
    const start = positions.get(hop.from_node_id);
    const end = positions.get(hop.to_node_id);
    if (start === undefined || end === undefined) continue;
    const line: RelayFeatureProperties = {
      kind: "route",
      certainty: hop.how === "heard_directly" ? "known" : "guess",
      estimated: false,
      color: ROUTE_COLOR,
      rotation: 0,
      label: null,
      nodeId: hop.to_node_id,
      active:
        focus === null ||
        (activeNodes.has(hop.from_node_id) && activeNodes.has(hop.to_node_id)),
      selected: false,
      reception: false,
    };
    features.push(
      {
        type: "Feature",
        geometry: { type: "LineString", coordinates: [start, end] },
        properties: line,
      },
      {
        type: "Feature",
        geometry: { type: "Point", coordinates: mercatorMidpoint(start, end) },
        properties: {
          ...line,
          kind: "route-arrow",
          color: hop.how === "zero_cost" ? zeroCostColor() : ROUTE_COLOR,
          rotation: arrowRotation(start, end),
        },
      },
    );
    relays.add(hop.from_node_id);
    relays.add(hop.to_node_id);
  }
  for (const report of selectedReports) {
    if (report.own_upload) continue;
    const fromId = report.direct ? sender : report.relay_node?.node_id;
    if (fromId === undefined || fromId === report.gateway_node_id) continue;
    const start = positions.get(fromId);
    const end = positions.get(report.gateway_node_id);
    if (start === undefined || end === undefined) continue;
    const line: RelayFeatureProperties = {
      kind: "route",
      certainty:
        report.direct || report.relay_node?.how === "heard_directly"
          ? "known"
          : "guess",
      estimated: false,
      color: linkTierColor(linkTier(report.rx_snr, detail.spreading_factor)),
      rotation: 0,
      label: null,
      nodeId: fromId,
      active: true,
      selected: false,
      reception: true,
    };
    features.push(
      {
        type: "Feature",
        geometry: { type: "LineString", coordinates: [start, end] },
        properties: line,
      },
      {
        type: "Feature",
        geometry: { type: "Point", coordinates: mercatorMidpoint(start, end) },
        properties: {
          ...line,
          kind: "route-arrow",
          rotation: arrowRotation(start, end),
        },
      },
    );
  }
  // The sender keeps its own marker.
  relays.delete(sender);
  for (const node of nodes.values()) {
    const at = positions.get(node.node_id);
    if (!relays.has(node.node_id) || at === undefined) continue;
    features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: at },
      properties: {
        kind: "relay",
        certainty: "known",
        estimated: node.estimated,
        color: "",
        rotation: 0,
        label: node.short_name ?? node.long_name,
        nodeId: node.node_id,
        active: focus === null || activeNodes.has(node.node_id),
        selected: focus?.kind === "relay" && focus.nodeId === node.node_id,
        reception: false,
      },
    });
  }
  return { type: "FeatureCollection", features };
}

/** Where the relay map places its relays, as [longitude, latitude]. */
export function relayPositions(
  relays: PacketRelays | null,
): [number, number][] {
  return (relays?.features ?? []).flatMap((feature) =>
    feature.properties.kind === "relay"
      ? [(feature.geometry as Point).coordinates as [number, number]]
      : [],
  );
}

function hopsOf(reception: PacketReception): number | null {
  if (reception.direct) return 0;
  if (reception.hop_start === null || reception.hop_limit === null) return null;
  const hops = reception.hop_start - reception.hop_limit;
  return hops >= 0 ? hops : null;
}
