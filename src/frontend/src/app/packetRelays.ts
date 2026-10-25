import type { Feature, FeatureCollection, LineString, Point } from "geojson";

import type { FloodRoutes, PacketDetail, PacketReception } from "./api";
import { zeroCostColor } from "./mapLinkLayers";
import { arrowRotation, mercatorMidpoint } from "./minimapMarkers";

interface RelayFeatureProperties {
  kind: "route" | "route-arrow" | "relay";
  certainty: "known" | "guess";
  estimated: boolean;
  color: string;
  rotation: number;
  label: string | null;
}

const ROUTE_COLOR = "#64748b";

export type PacketRelays = FeatureCollection<
  LineString | Point,
  RelayFeatureProperties
>;

/** Relays and likely connections; null for traceroutes or packets heard only directly. */
export function packetRelays(
  detail: PacketDetail,
  routes: FloodRoutes | null = null,
): PacketRelays | null {
  if (
    detail.traceroute !== null ||
    !detail.receptions.some(
      (reception) => !reception.own_upload && (hopsOf(reception) ?? 0) > 0,
    )
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
