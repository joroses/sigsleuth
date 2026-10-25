import { describe, expect, it } from "vitest";

import type {
  FloodRoutes,
  NodeSummary,
  PacketDetail,
  PacketReception,
} from "./api";
import { packetRelays } from "./packetRelays";

const SENDER = 0x11111111;
const RELAY = 0x33333333;
const OTHER_RELAY = 0x44444444;
const ROUTER = 0x55555555;

function heard(
  reception_id: number,
  hops: number | null,
  changes: Partial<PacketReception> = {},
): PacketReception {
  return {
    reception_id,
    received_at: "2026-09-19T09:00:00Z",
    gateway_node_id: 0xbbbbbb00 + reception_id,
    gateway_name: null,
    gateway_short_name: null,
    gateway_latitude: 52,
    gateway_longitude: 19 + reception_id / 10,
    gateway_estimated: false,
    rx_snr: 5,
    rx_rssi: -100,
    hop_start: hops === null ? null : 7,
    hop_limit: hops === null ? null : 7 - hops,
    relay_byte: hops ? 0x33 : null,
    paths: null,
    hops: [],
    reached: false,
    direct: hops === 0,
    own_upload: false,
    relay_node: null,
    relay_maybe: [],
    km: null,
    ...changes,
  };
}

function relay(node_id: number, short_name: string): NodeSummary {
  return {
    node_id,
    long_name: null,
    short_name,
    role: null,
    latitude: 51,
    longitude: 18 + node_id / 0x11111111,
    estimated: false,
  };
}

function detail(
  receptions: PacketReception[],
  changes: Partial<PacketDetail> = {},
): PacketDetail {
  return {
    packet: {
      packet_row_id: 7,
      first_received_at: "2026-09-19T09:00:00Z",
      last_received_at: "2026-09-19T09:00:02Z",
      reception_count: receptions.length,
      channel: "LongFast",
      sender_node_id: SENDER,
      sender_name: "Alice",
      recipient_node_id: 0xffffffff,
      recipient_name: null,
      packet_id: 1234,
      portnum: "TEXT_MESSAGE_APP",
    },
    receptions,
    payload: null,
    sender_latitude: 52,
    sender_longitude: 18,
    sender_estimated: false,
    unreadable_reason: null,
    unreadable_reason_description: null,
    hops: [],
    route_nodes: [],
    relay_nodes: [relay(RELAY, "R1"), relay(OTHER_RELAY, "R2")],
    spreading_factor: 11,
    traceroute: null,
    reply_route: null,
    hop_costs: [],
    ...changes,
  };
}

const named = (
  node_id: number,
  how: NonNullable<PacketReception["relay_node"]>["how"] = "heard_directly",
) => ({
  relay_node: { node_id, name: null, candidates: 1, how },
});

function lines(flood: NonNullable<ReturnType<typeof packetRelays>>) {
  return flood.features.flatMap((feature) =>
    feature.geometry.type === "LineString"
      ? [
          [
            feature.geometry.coordinates[0][0],
            feature.geometry.coordinates[1][0],
            feature.properties.certainty,
          ],
        ]
      : [],
  );
}

function hop(
  from_node_id: number,
  to_node_id: number,
  step: number,
  how: FloodRoutes["hops"][number]["how"] = "probable",
): FloodRoutes["hops"][number] {
  return {
    from_node_id,
    to_node_id,
    step,
    how,
    probability: how === "probable" ? 0.9 : null,
  };
}

describe("packetRelays", () => {
  it("draws a relay that is also a gateway at its gateway position", () => {
    const gateway = heard(1, 0);
    const flood = packetRelays(
      detail([gateway, heard(2, 1, named(gateway.gateway_node_id))], {
        relay_nodes: [],
      }),
    )!;
    expect(
      flood.features.find((feature) => feature.properties.kind === "relay")
        ?.geometry,
    ).toEqual({ type: "Point", coordinates: [19.1, 52] });
  });

  it("keeps a relay's estimated position on its dot", () => {
    const flood = packetRelays(
      detail([heard(1, 0), heard(2, 1, named(RELAY))], {
        relay_nodes: [{ ...relay(RELAY, "R1"), estimated: true }],
      }),
    )!;
    const dot = flood.features.find(
      (feature) => feature.properties.kind === "relay",
    );
    expect(dot?.properties.estimated).toBe(true);
  });

  it("draws the likely route back to the sender", () => {
    const flood = packetRelays(
      detail([heard(1, 0), heard(2, 2, named(RELAY))]),
      {
        hops: [
          hop(SENDER, OTHER_RELAY, 0, "one_hop"),
          hop(OTHER_RELAY, RELAY, 1),
        ],
        nodes: [],
      },
    )!;
    expect(lines(flood)).toEqual([
      [18, 22, "guess"],
      [22, 21, "guess"],
    ]);
    const dots = flood.features.filter(
      (feature) => feature.properties.kind === "relay",
    );
    expect(dots.map((dot) => dot.properties.label)).toEqual(["R1", "R2"]);
  });

  it("draws a route line from a relay's own direct reception as known", () => {
    const flood = packetRelays(
      detail([heard(1, 0), heard(2, 2, named(RELAY))]),
      { hops: [hop(OTHER_RELAY, RELAY, 1, "heard_directly")], nodes: [] },
    )!;
    expect(lines(flood)).toEqual([[22, 21, "known"]]);
  });

  it("dots a route's own node, hollow when estimated, and skips unplaced ends", () => {
    const router = { ...relay(ROUTER, "RT"), estimated: true };
    const flood = packetRelays(
      detail([heard(1, 0), heard(2, 3, named(RELAY))]),
      {
        hops: [hop(ROUTER, RELAY, 2), hop(0x66666666, ROUTER, 1)],
        nodes: [router],
      },
    )!;
    expect(lines(flood)).toEqual([[23, 21, "guess"]]);
    const dot = flood.features.find(
      (feature) =>
        feature.properties.kind === "relay" &&
        feature.properties.label === "RT",
    );
    expect(dot?.properties.estimated).toBe(true);
  });

  it("offers no relay map for traceroutes or packets heard only directly", () => {
    expect(
      packetRelays(
        detail([heard(1, 0), heard(2, 1, named(RELAY))], {
          traceroute: { requester_node_id: SENDER, target_node_id: RELAY },
        }),
      ),
    ).toBeNull();
    expect(packetRelays(detail([heard(1, 0), heard(2, 0)]))).toBeNull();
  });
});
