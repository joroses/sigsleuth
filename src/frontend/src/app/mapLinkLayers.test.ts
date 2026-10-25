import { describe, expect, it } from "vitest";

import type { MapLink, MapNode } from "./api";
import {
  buildLinkFeatureCollection,
  drawnLongEnough,
  linkTierShown,
  linkKey,
  withinHops,
  zeroCostArrows,
} from "./mapLinkLayers";
import { linkWidth } from "./linkQuality";

function mapNode(node_id: number, latitude: number, longitude: number) {
  return {
    node_id,
    long_name: null,
    short_name: null,
    role: null,
    hardware_model: null,
    last_sent_at: "2026-09-25T12:00:00Z",
    last_sent_packet_row_id: 1,
    last_seen_at: "2026-09-25T12:00:00Z",
    channel: "LongFast",
    last_gateway_at: null,
    latitude,
    longitude,
    estimate: null,
    position: {
      observed_at: "2026-09-25T12:00:00Z",
      device_time: null,
      latitude,
      longitude,
      altitude_meters: null,
      packet_row_id: 1,
      from_map_report: false,
      precision_km: null,
    },
  } satisfies MapNode;
}

const link: MapLink = {
  node_a: 1,
  node_b: 2,
  last_seen_at: "2026-09-25T12:00:00Z",
  channel: "LongFast",
  spreading_factor: 11,
  a_to_b: { observations: 9, snr_samples: 9, average_snr: -2 },
  b_to_a: { observations: 1, snr_samples: 0, average_snr: null },
};

describe("zeroCostArrows", () => {
  // Node 2 due north of node 1: east is 0°, clockwise on screen.
  const nodes = new Map([
    [1, mapNode(1, 50, 20)],
    [2, mapNode(2, 51, 20)],
  ]);
  const free = (from: number, to: number, channel = "LongFast") => ({
    from_node_id: from,
    to_node_id: to,
    channel,
    free: 2,
    costly: 0,
  });
  const arrows = (hops: ReturnType<typeof free>[]) =>
    zeroCostArrows([link], hops, nodes).features.map(
      ({ properties: { rotation, both } }) => ({ rotation, both }),
    );

  it("points the free way, only on the hop's channel", () => {
    expect(arrows([free(1, 2)])).toEqual([{ rotation: -90, both: false }]);
    expect(arrows([free(2, 1)])).toEqual([{ rotation: 90, both: false }]);
    expect(arrows([free(1, 2, "MediumFast")])).toEqual([]);
  });

  it("puts two back to back when free both ways", () => {
    expect(arrows([free(1, 2), free(2, 1)])).toEqual([
      { rotation: -90, both: true },
      { rotation: 90, both: true },
    ]);
  });
});

describe("buildLinkFeatureCollection", () => {
  it("draws two halves, each showing what the node at its end hears", () => {
    const nodes = new Map([
      [1, mapNode(1, 0, 0)],
      [2, mapNode(2, 60, 10)],
    ]);

    const data = buildLinkFeatureCollection("traceroute", [link], nodes);

    const [nearA, nearB] = data.features;
    expect(nearA.geometry.coordinates[0]).toEqual([0, 0]);
    expect(nearA.properties.tier).toBe("unknown");
    expect(nearB.geometry.coordinates[1]).toEqual([10, 60]);
    expect(nearB.properties.tier).toBe("good");
    // Both halves share one width from 10 observations in total.
    expect(nearA.properties.width).toBe(linkWidth(10));
    expect(nearB.properties.width).toBe(linkWidth(10));
    // The halves meet on screen, which in Mercator lies north of 30°.
    const middle = nearA.geometry.coordinates[1];
    expect(nearB.geometry.coordinates[0]).toEqual(middle);
    expect(middle[0]).toBe(5);
    expect(middle[1]).toBeGreaterThan(35);
  });

  it("draws a pair heard on two channels as two lines, each rated by its channel", () => {
    const nodes = new Map([
      [1, mapNode(1, 0, 0)],
      [2, mapNode(2, 60, 10)],
    ]);
    const narrow = { ...link, channel: "SFNarrow", spreading_factor: 7 };

    const data = buildLinkFeatureCollection(
      "traceroute",
      [link, narrow],
      nodes,
    );

    const nearB = data.features.filter((_, index) => index % 2 === 1);
    expect(nearB.map((half) => half.properties.tier)).toEqual(["good", "fair"]);
    expect(new Set(nearB.map((half) => half.properties.linkKey)).size).toBe(2);
  });

  it("leaves out links to a node with no position", () => {
    const data = buildLinkFeatureCollection(
      "traceroute",
      [link],
      new Map([[1, mapNode(1, 0, 0)]]),
    );

    expect(data.features).toEqual([]);
  });
});

describe("withinHops", () => {
  // A chain 1 – 2 – 3 – 4 – 5, a traceroute link 2 – 6 and a direct link 6 – 3
  // closing a loop, and 7 – 8 on their own.
  const pair = (node_a: number, node_b: number) => ({
    ...link,
    node_a,
    node_b,
  });
  const links = [
    { linkKind: "traceroute" as const, link: pair(1, 2) },
    { linkKind: "traceroute" as const, link: pair(2, 3) },
    { linkKind: "traceroute" as const, link: pair(3, 4) },
    { linkKind: "traceroute" as const, link: pair(4, 5) },
    { linkKind: "traceroute" as const, link: pair(2, 6) },
    { linkKind: "direct" as const, link: pair(3, 6) },
    { linkKind: "traceroute" as const, link: pair(7, 8) },
  ];
  // Each kept link as its kind and node pair, in the order of `links`.
  const sorted = (result: { nodeIds: number[]; linkKeys: string[] }) => ({
    nodeIds: [...result.nodeIds].sort(),
    links: links
      .filter(({ linkKind, link }) =>
        result.linkKeys.includes(linkKey(linkKind, link)),
      )
      .map(({ linkKind, link }) => [linkKind, link.node_a, link.node_b]),
  });

  it("keeps only the node's own links and neighbours at depth 0", () => {
    // 2 – 6 joins two neighbours, not the node itself, so it is left out.
    expect(sorted(withinHops(3, links, 0))).toEqual({
      nodeIds: [2, 3, 4, 6],
      links: [
        ["traceroute", 2, 3],
        ["traceroute", 3, 4],
        ["direct", 3, 6],
      ],
    });
  });

  it("adds one relay per step over both kinds of link, whichever end is named first", () => {
    // 1 and 5 are two links away.
    expect(sorted(withinHops(3, links, 1))).toEqual({
      nodeIds: [1, 2, 3, 4, 5, 6],
      links: [
        ["traceroute", 1, 2],
        ["traceroute", 2, 3],
        ["traceroute", 3, 4],
        ["traceroute", 4, 5],
        ["traceroute", 2, 6],
        ["direct", 3, 6],
      ],
    });
    // Nodes it can never reach stay out however deep.
    expect(withinHops(3, links, 7).nodeIds).not.toContain(7);
  });

  it("keeps just the node when it has no links", () => {
    expect(withinHops(9, links, 3)).toEqual({ nodeIds: [9], linkKeys: [] });
  });
});

describe("linkTierShown", () => {
  const onlyGood = { good: true, fair: false, marginal: false };
  const withSnr = (aToB: number | null, bToA: number | null): MapLink => ({
    ...link,
    a_to_b: { ...link.a_to_b, average_snr: aToB },
    b_to_a: { ...link.b_to_a, average_snr: bToA },
  });

  it("keeps a link when either half is of a chosen tier", () => {
    // At SF 11 the floor is -17.5 dB: -2 is good, -16 marginal.
    expect(linkTierShown(onlyGood, withSnr(-2, -16))).toBe(true);
    expect(linkTierShown(onlyGood, withSnr(-16, -16))).toBe(false);
  });

  it("does not let a half without an SNR keep a link", () => {
    expect(linkTierShown(onlyGood, withSnr(-16, null))).toBe(false);
    expect(linkTierShown(onlyGood, withSnr(-2, null))).toBe(true);
    expect(linkTierShown(onlyGood, withSnr(null, null))).toBe(true);
  });
});

describe("drawnLongEnough", () => {
  // A degree of latitude is about 111 km.
  const reported = mapNode(1, 40, -3);
  const estimated = {
    ...mapNode(2, 41, -3),
    position: null,
    estimate: {
      node_id: 2,
      latitude: 41,
      longitude: -3,
      neighbours: 1,
      spread_km: 0,
      from_traceroute: false,
    },
  } satisfies MapNode;
  const drawn = new Map<number, MapNode>([
    [1, reported],
    [2, estimated],
  ]);

  it("measures where the map draws both ends, an estimate included", () => {
    expect(drawnLongEnough(link, drawn, 110)).toBe(true);
    expect(drawnLongEnough(link, drawn, 115)).toBe(false);
  });

  it("hides a link with an end not drawn once a minimum is set", () => {
    const one = new Map<number, MapNode>([[1, reported]]);
    expect(drawnLongEnough(link, one, 0)).toBe(true);
    expect(drawnLongEnough(link, one, 5)).toBe(false);
  });
});
