import { describe, expect, it } from "vitest";

import type { MapNode } from "./api";
import {
  type NodeFeatureCollection,
  buildNodeFeatureCollection,
  nodesBounds,
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
      3: [true, false, "2×"],
      4: [false, false, ""],
    });
  });
});

describe("nodesBounds", () => {
  const at = (latitude: number, longitude: number) => ({ latitude, longitude });

  it("is null without nodes", () => {
    expect(nodesBounds([])).toBeNull();
  });

  it("covers every node when there are few", () => {
    expect(nodesBounds([at(52, 21), at(50, 19), at(37, -6)])).toEqual([
      [-6, 37],
      [21, 52],
    ]);
  });

  it("leaves out the outermost 1% on each side", () => {
    const nodes = Array.from({ length: 100 }, (_, i) =>
      at(50 + i / 100, 15 + i / 100),
    );
    nodes[0] = at(37, -6); // One node far away.
    expect(nodesBounds(nodes)).toEqual([
      [15.01, 50.01],
      [15.98, 50.98],
    ]);
  });
});
