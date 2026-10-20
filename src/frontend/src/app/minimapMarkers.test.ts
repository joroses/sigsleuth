import { describe, expect, it } from "vitest";

import type {
  NodeSummary,
  TracerouteHop,
  PacketDetail,
  PacketReception,
} from "./api";
import {
  routeMinimap,
  minimapMarkers,
  routeNumbers,
  reportsThrough,
  reportsWithHop,
} from "./minimapMarkers";

const reception: PacketReception = {
  reception_id: 1,
  received_at: "2026-09-19T09:00:00Z",
  gateway_node_id: 0xbbbbbbbb,
  gateway_name: "Bob",
  gateway_short_name: "BOB",
  gateway_latitude: 51.5,
  gateway_longitude: -0.1,
  gateway_estimated: false,
  rx_snr: 6.5,
  rx_rssi: -97,
  hop_start: 3,
  hop_limit: 3,
  relay_byte: null,
  paths: null,
  hops: [],
  reached: false,
  direct: true,
  own_upload: false,
  relay_node: null,
  relay_maybe: [],
  km: null,
};

function detail(
  receptions: PacketReception[],
  senderPosition: [number, number] | null,
  hops: TracerouteHop[] = [],
  routeNodes: NodeSummary[] = [],
  traceroute: PacketDetail["traceroute"] = null,
): PacketDetail {
  return {
    packet: {
      packet_row_id: 7,
      first_received_at: "2026-09-19T09:00:00Z",
      last_received_at: "2026-09-19T09:00:02Z",
      reception_count: receptions.length,
      channel: "LongFast",
      sender_node_id: 0x11111111,
      sender_name: "Alice",
      recipient_node_id: 0xffffffff,
      recipient_name: null,
      packet_id: 1234,
      portnum: null,
    },
    receptions,
    payload: null,
    sender_latitude: senderPosition?.[0] ?? null,
    sender_longitude: senderPosition?.[1] ?? null,
    sender_estimated: false,
    unreadable_reason: null,
    unreadable_reason_description: null,
    hops,
    route_nodes: routeNodes,
    spreading_factor: 11,
    traceroute,
    reply_route: null,
  };
}

describe("minimapMarkers", () => {
  it("labels the sender S and each gateway with the hops it used", () => {
    const { markers, unplacedGateways } = minimapMarkers(
      detail(
        [
          reception,
          { ...reception, reception_id: 2, hop_limit: 1, direct: false },
          { ...reception, reception_id: 3, hop_start: null, direct: false },
          { ...reception, reception_id: 4, direct: false, own_upload: true },
          {
            ...reception,
            reception_id: 5,
            direct: false,
            own_upload: true,
            gateway_latitude: null,
            gateway_longitude: null,
          },
        ],
        [51.4, -0.2],
      ),
    );
    // The sender's own uploads are not drawn or counted: they sit on S.
    expect(markers.map((marker) => [marker.label, marker.tone])).toEqual([
      ["S", "hop-tone-sender"],
      ["0", "hop-tone-direct"],
      ["2", "hop-tone-2"],
      ["?", "hop-tone-unknown"],
    ]);
    expect(unplacedGateways).toBe(0);
  });

  it("leaves out and counts gateways without a position", () => {
    const { markers, unplacedGateways } = minimapMarkers(
      detail(
        [
          reception,
          {
            ...reception,
            reception_id: 2,
            gateway_latitude: null,
            gateway_longitude: null,
            gateway_estimated: false,
          },
        ],
        null,
      ),
    );
    expect(markers.map((marker) => marker.key)).toEqual(["reception-1"]);
    expect(unplacedGateways).toBe(1);
  });

  it("draws a gateway placed only by an estimate, marked as estimated", () => {
    const { markers, unplacedGateways } = minimapMarkers(
      detail(
        [reception, { ...reception, reception_id: 2, gateway_estimated: true }],
        null,
      ),
    );
    expect(markers.map((marker) => [marker.key, marker.estimated])).toEqual([
      ["reception-1", false],
      ["reception-2", true],
    ]);
    expect(unplacedGateways).toBe(0);
  });

  describe("traceroutes", () => {
    const requester = 0x0a0a0a0a;
    const target = 0x11111111;
    const relay = 0x22222222;
    const otherRelay = 0x33333333;
    const ends = { requester_node_id: requester, target_node_id: target };
    const node = (node_id: number, latitude: number | null): NodeSummary => ({
      node_id,
      long_name: `Node ${node_id}`,
      short_name: null,
      role: null,
      latitude,
      longitude: latitude === null ? null : 0,
      estimated: false,
    });
    const hop = (from: number, to: number, snr: number | null = 1) => ({
      from_node_id: from,
      to_node_id: to,
      snr_db: snr,
    });
    const lines = (hops: {
      features: {
        features: { geometry: { type: string }; properties: unknown }[];
      };
    }) =>
      hops.features.features.filter(
        (feature) => feature.geometry.type === "LineString",
      );

    it("marks S at the requester, R at the target and relays, but no gateways", () => {
      // A reply: the packet's sender is the target, its recipient the requester.
      const { markers, unplacedGateways } = minimapMarkers(
        detail(
          [
            reception,
            { ...reception, reception_id: 2, gateway_latitude: null },
          ],
          [51.4, -0.2],
          [],
          [
            node(requester, 51.3),
            node(target, 51.4),
            node(relay, 51.35),
            node(otherRelay, null),
          ],
          ends,
        ),
      );
      expect(
        markers.map((marker) => [marker.kind, marker.label, marker.nodeId]),
      ).toEqual([
        ["sender", "S", requester],
        ["target", "R", target],
        ["relay", "", relay],
      ]);
      expect(markers[0].title).toBe(`Traceroute from: Node ${requester}`);
      expect(unplacedGateways).toBe(0);
    });

    it("draws a pair crossed both ways as two two-way lines and counts hops it cannot draw", () => {
      const { allHops } = minimapMarkers(
        detail(
          [],
          null,
          [
            hop(requester, relay),
            hop(relay, requester, null),
            hop(relay, target),
            hop(target, otherRelay),
          ],
          [node(requester, 51.3), node(target, 51.4), node(relay, 51.35)],
          ends,
        ),
      );
      expect(lines(allHops).map((line) => line.properties)).toMatchObject([
        { twoWay: true, snr: 1 },
        { twoWay: true, snr: null, tier: "unknown" },
        { twoWay: false },
      ]);
      expect([allHops.undrawnHops, allHops.hops]).toEqual([1, 4]);
    });

    it("keeps each reception's own route apart from all hops", () => {
      const paths = {
        forward: { nodes: [requester, relay, target], snr_db: [1] },
        return_path: null,
      };
      const { allHops, receptionHops, receptionNumbers } = minimapMarkers(
        detail(
          [
            { ...reception, paths, hops: [hop(requester, relay)] },
            { ...reception, reception_id: 3 },
          ],
          null,
          [hop(requester, relay), hop(requester, target)],
          [node(requester, 51.3), node(target, 51.4), node(relay, 51.35)],
          ends,
        ),
      );
      expect(lines(allHops)).toHaveLength(2);
      expect(lines(receptionHops.get(1)!)).toHaveLength(1);
      expect(receptionNumbers.get(1)).toEqual(new Map([[relay, "1"]]));
      // A reception without a route has nothing to focus.
      expect(receptionHops.has(3)).toBe(false);
    });

    it("finds the reports through a relay, on either route of a reply", () => {
      const reply = (
        reception_id: number,
        returned: (number | null)[],
        hops: TracerouteHop[],
      ) => ({
        ...reception,
        reception_id,
        paths: {
          forward: { nodes: [requester, relay, target], snr_db: [1, 2] },
          return_path: { nodes: returned, snr_db: [] },
        },
        hops,
      });
      const shared = [hop(requester, relay), hop(relay, target)];
      const { receptionIds, hops } = reportsThrough(
        detail(
          [
            reply(
              1,
              [target, otherRelay, requester],
              [...shared, hop(target, otherRelay)],
            ),
            reply(2, [target, requester], shared),
            { ...reception, reception_id: 3 },
          ],
          null,
        ),
        relay,
      );
      // Every reply crossed the relay on its forward route; shared hops count once.
      expect(receptionIds).toEqual(new Set([1, 2]));
      expect(hops).toHaveLength(3);
      expect(
        reportsThrough(
          detail([reply(1, [target, otherRelay, requester], [])], null),
          otherRelay,
        ).receptionIds,
      ).toEqual(new Set([1]));
    });

    it("finds the reports with a hop in its direction, on a return route or a reply route", () => {
      // A reply heard back from the target through the relay, and a
      // request's own route that crossed the relay the other way.
      const reply = {
        ...reception,
        reception_id: 1,
        paths: {
          forward: { nodes: [requester, target], snr_db: [1] },
          return_path: { nodes: [target, relay, requester], snr_db: [2, 3] },
        },
        hops: [
          hop(requester, target),
          hop(target, relay),
          hop(relay, requester),
        ],
      };
      const request = {
        ...reception,
        reception_id: 2,
        paths: {
          forward: { nodes: [requester, relay], snr_db: [4] },
          return_path: null,
        },
        hops: [hop(requester, relay)],
      };
      const withReply = {
        ...detail([reply, request], null),
        reply_route: {
          packet_row_id: 8,
          reception_id: 9,
          path: { nodes: [requester, relay, target], snr_db: [4, 5] },
          hops: [hop(requester, relay, 4), hop(relay, target, 5)],
        },
      };

      expect(reportsWithHop(withReply, relay, requester).receptionIds).toEqual(
        new Set([1]),
      );
      // The opposite way: the request, and the route its reply reports.
      const found = reportsWithHop(withReply, requester, relay);
      expect(found.receptionIds).toEqual(new Set([2, 9]));
      expect(found.nodeIds).toEqual(new Set([requester, relay, target]));
    });

    it("numbers a reply's relays through the round trip", () => {
      const numbers = routeNumbers(
        {
          forward: {
            nodes: [requester, relay, null, target],
            snr_db: [1, null, 2],
          },
          return_path: {
            nodes: [target, otherRelay, relay, requester],
            snr_db: [],
          },
        },
        requester,
        target,
      );
      // The gap is position 2 and the target 3; S and R get no number.
      expect(numbers).toEqual(
        new Map([
          [relay, [1, 5]],
          [otherRelay, [4]],
        ]),
      );
    });

    it("maps a return route with S at the requester and relays numbered along it", () => {
      const placed = (node_id: number, latitude: number): NodeSummary => ({
        ...node(node_id, latitude),
        role: null,
      });
      const contents = routeMinimap(
        {
          requester_node_id: requester,
          target_node_id: target,
          first_received_at: "2026-09-19T09:00:00Z",
          channel: "LongFast",
          spreading_factor: 11,
          request_packet_row_id: 6,
          request_receptions: 1,
          reply_packet_row_id: 7,
          reply_receptions: 2,
          distance_km: null,
          doubts: [],
          // The relay was farther south when the route was heard.
          positions: [
            {
              node_id: relay,
              latitude: 51.2,
              longitude: 0,
              altitude_meters: null,
            },
          ],
          route: {
            packet_row_id: 7,
            is_return: true,
            hops: 3,
            km: 12.5,
            reached: false,
            // Back from the target: otherRelay is heard first, then relay.
            path: {
              nodes: [target, otherRelay, relay, requester],
              snr_db: [4, null, 6],
            },
            path_hops: [
              hop(target, otherRelay, 4),
              hop(otherRelay, relay, null),
              hop(relay, requester, 6),
            ],
            hop: null,
            unlikely: false,
          },
        },
        new Map(
          [
            placed(requester, 51.3),
            placed(target, 51.4),
            placed(relay, 51.33),
            placed(otherRelay, 51.37),
          ].map((item) => [item.node_id, item]),
        ),
      );
      expect(
        contents.markers.map((marker) => [
          marker.label,
          marker.nodeId,
          marker.latitude,
        ]),
      ).toEqual([
        ["S", requester, 51.3],
        ["R", target, 51.4],
        ["1", otherRelay, 51.37],
        ["2", relay, 51.2],
      ]);
      expect(
        lines(contents.allHops).map((line) => line.properties),
      ).toMatchObject([{ snr: 4 }, { snr: null }, { snr: 6 }]);
      expect(contents.labelAllHops).toBe(true);
    });
  });
});
