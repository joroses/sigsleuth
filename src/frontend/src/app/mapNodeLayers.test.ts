import { describe, expect, it } from "vitest";

import type { MapNode } from "./api";
import {
  type NodeFeatureCollection,
  buildNodeFeatureCollection,
  nodeCategory,
} from "./mapNodeLayers";

const node: MapNode = {
  node_id: 0xdeadbeef,
  long_name: "Home Base",
  short_name: null,
  role: null,
  hardware_model: null,
  last_sent_at: "2026-09-25T12:00:00Z",
  last_sent_packet_row_id: 2,
  last_seen_at: "2026-09-25T12:00:00Z",
  channel: "LongFast",
  last_gateway_at: null,
  latitude: 40.4,
  longitude: -3.7,
  estimate: null,
  position: {
    observed_at: "2026-09-25T12:00:00Z",
    device_time: null,
    latitude: 40.4,
    longitude: -3.7,
    altitude_meters: null,
    packet_row_id: 1,
    from_map_report: false,
    precision_km: null,
  },
};

describe("nodeCategory", () => {
  const gateway = { ...node, last_gateway_at: "2026-09-25T12:00:00Z" };

  it("colours a router that is also a gateway as a relay", () => {
    expect(nodeCategory({ ...gateway, role: "ROUTER" })).toBe("relay");
    expect(nodeCategory({ ...node, role: "REPEATER" })).toBe("relay");
  });

  it("colours any other gateway as a gateway", () => {
    expect(nodeCategory({ ...gateway, role: "CLIENT" })).toBe("gateway");
    expect(nodeCategory(gateway)).toBe("gateway");
    expect(nodeCategory({ ...node, role: "CLIENT" })).toBe("other");
  });
});

describe("buildNodeFeatureCollection", () => {
  it("draws reported and estimated positions in longitude-latitude order", () => {
    const { features } = buildNodeFeatureCollection([
      node,
      {
        ...node,
        node_id: 0xcafef00d,
        position: null,
        latitude: 41,
        longitude: -3,
        estimate: {
          node_id: 0xcafef00d,
          latitude: 41,
          longitude: -3,
          neighbours: 2,
          spread_km: 4,
          from_traceroute: false,
        },
      },
    ]);

    expect(
      features.map((feature) => [
        feature.geometry.coordinates,
        feature.properties.estimated,
      ]),
    ).toEqual([
      [[-3.7, 40.4], false],
      [[-3, 41], true],
    ]);
  });

  // 1 sends; 2 is its neighbour and a gateway; 3 a gateway with no link; 4
  // neither.
  const nodes = [1, 2, 3, 4].map((node_id) => ({ ...node, node_id }));
  const shares = [
    { nodeId: 2, receptions: 9, messages: 10 },
    { nodeId: 3, receptions: 2, messages: 10 },
  ];
  const view = (features: NodeFeatureCollection) =>
    Object.fromEntries(
      features.features.map(({ properties: p }) => [
        p.nodeId,
        [p.shown, p.selected, p.badge],
      ]),
    );

  it("keeps the focus and the gateways, badges them and marks the selection", () => {
    expect(
      view(
        buildNodeFeatureCollection(nodes, {
          focus: new Set([1, 2]),
          selectedId: 1,
          shares,
        }),
      ),
    ).toEqual({
      1: [true, true, ""],
      2: [true, false, "90%"],
      3: [true, false, "20%"],
      4: [false, false, ""],
    });
  });

  it("keeps every node without a focus", () => {
    const { features } = buildNodeFeatureCollection(nodes, {
      focus: null,
      selectedId: 1,
      shares,
    });
    expect(features.every((feature) => feature.properties.shown)).toBe(true);
  });
});
