import { describe, expect, it } from "vitest";

import type { ReplyRoute, PacketDetail, PacketReception } from "./api";
import { sharedForwardRoute, tracerouteGroups } from "./tracerouteRows";

const A = 0xa,
  B = 0xb,
  C = 0xc,
  D = 0xd,
  E = 0xe,
  T = 0x7;

// A request from A to T that gateway `gateway` heard after `relays`, using
// one hop per relay.
function report(
  id: number,
  relays: number[],
  gateway = 0x100 + id,
): PacketReception {
  return {
    reception_id: id,
    received_at: "2026-09-19T09:00:00Z",
    gateway_node_id: gateway,
    gateway_name: null,
    gateway_short_name: null,
    gateway_latitude: null,
    gateway_longitude: null,
    gateway_estimated: false,
    rx_snr: null,
    rx_rssi: null,
    hop_start: 7,
    hop_limit: 7 - relays.length,
    relay_byte: null,
    paths: {
      forward: { nodes: [A, ...relays, T], snr_db: relays.map(() => 5) },
      return_path: null,
    },
    hops: [],
    // As the API says of a request the target's own gateway reported.
    reached: gateway === T,
    direct: false,
    own_upload: false,
    relay_node: null,
    relay_maybe: [],
    km: null,
  };
}

function request(
  receptions: PacketReception[],
  reply: ReplyRoute | null = null,
): PacketDetail {
  return {
    packet: {
      packet_row_id: 1,
      first_received_at: "2026-09-19T09:00:00Z",
      last_received_at: "2026-09-19T09:00:02Z",
      reception_count: receptions.length,
      channel: "LongFast",
      sender_node_id: A,
      sender_name: null,
      recipient_node_id: T,
      recipient_name: null,
      sender_radio: null,
      packet_id: 99,
      portnum: "TRACEROUTE_APP",
    },
    receptions,
    payload: null,
    sender_latitude: null,
    sender_longitude: null,
    sender_estimated: false,
    unreadable_reason: null,
    unreadable_reason_description: null,
    hops: [],
    route_nodes: [],
    relay_nodes: [],
    spreading_factor: 11,
    traceroute: { requester_node_id: A, target_node_id: T },
    reply_route: reply,
    hop_costs: [],
  };
}

// Each group as its label and its rows' reception IDs (0 for the reply) with
// how many hidden reports they stand for.
function summary(detail: PacketDetail) {
  const { groups, hidden } = tracerouteGroups(detail);
  return {
    groups: groups.map(({ label, rows }) => [
      label,
      rows.map((row) => [
        row.kind === "reply" ? 0 : row.reception.reception_id,
        row.alsoReported,
      ]),
    ]),
    hidden,
  };
}

describe("tracerouteGroups", () => {
  it("keeps only the longest routes, most hops first", () => {
    expect(
      summary(
        request([
          report(1, [B]),
          report(2, [B, C]),
          report(3, [B, D]),
          report(4, [B, C]),
          report(5, [C, D, E]),
        ]),
      ),
    ).toEqual({
      groups: [
        ["3 hops", [[5, 0]]],
        [
          "2 hops",
          [
            [2, 2],
            [3, 0],
          ],
        ],
      ],
      hidden: 2,
    });
  });

  it("puts the route the reply reports first, over its start", () => {
    const reply = {
      packet_row_id: 2,
      reception_id: 50,
      path: { nodes: [A, B, T], snr_db: [5, 6] },
      hops: [],
    };
    expect(
      summary(request([report(1, [B]), report(2, [C, D, E])], reply)),
    ).toEqual({
      groups: [
        ["Reached the target", [[0, 1]]],
        ["3 hops", [[2, 0]]],
      ],
      hidden: 1,
    });
  });

  it("keeps a report from the target itself even under a longer route", () => {
    expect(
      summary(request([report(1, [B], T), report(2, [B, C])])).groups,
    ).toEqual([
      ["Reached the target", [[1, 0]]],
      ["2 hops", [[2, 0]]],
    ]);
  });
});

describe("sharedForwardRoute", () => {
  // T's reply to A, as a gateway heard it: the forward route via relays, and
  // the route back so far.
  function replyReport(id: number, relays: number[], back: number[]) {
    const reception = report(id, relays);
    reception.paths = {
      forward: {
        nodes: [A, ...relays, T],
        snr_db: [...relays, T].map(() => 5),
      },
      return_path: { nodes: [T, ...back, A], snr_db: back.map(() => 3) },
    };
    return reception;
  }

  function reply(receptions: PacketReception[]): PacketDetail {
    const detail = request(receptions);
    detail.packet.sender_node_id = T;
    detail.packet.recipient_node_id = A;
    return detail;
  }

  it("takes the route to the target most reports share", () => {
    const detail = reply([
      replyReport(1, [B, C], [D]),
      replyReport(2, [E], []),
      replyReport(3, [B, C], [D, E]),
    ]);

    expect(sharedForwardRoute(detail)?.nodes).toEqual([A, B, C, T]);
  });
});
