import { describe, expect, it } from "vitest";

import type { MapLink } from "./api";
import { findRoute, withinHops } from "./linkGraph";
import { linkKey } from "./mapLinkLayers";

const link: MapLink = {
  node_a: 1,
  node_b: 2,
  last_seen_at: "2026-09-25T12:00:00Z",
  channel: "LongFast",
  spreading_factor: 11,
  a_to_b: {
    observations: 9,
    snr_samples: 9,
    average_snr: -2,
    average_rssi: null,
  },
  b_to_a: {
    observations: 1,
    snr_samples: 0,
    average_snr: null,
    average_rssi: null,
  },
};

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

describe("findRoute", () => {
  // A link heard `out` times node_a → node_b and `back` times the other way.
  const heard = (node_a: number, node_b: number, out: number, back = out) => ({
    link: {
      ...link,
      node_a,
      node_b,
      a_to_b: { ...link.a_to_b, observations: out },
      b_to_a: { ...link.b_to_a, observations: back },
    },
  });
  const pairs = (route: { links: { link: MapLink }[] } | null) =>
    route?.links.map(({ link }) => [link.node_a, link.node_b]) ?? null;

  it("takes a longer way over often heard links rather than one heard once", () => {
    const longWay = [heard(1, 3, 20), heard(3, 4, 20), heard(4, 5, 20)];
    expect(
      pairs(findRoute(1, 5, [heard(1, 2, 1), heard(2, 5, 20), ...longWay])),
    ).toEqual([
      [1, 3],
      [3, 4],
      [4, 5],
    ]);
    // Heard as often, the shorter way wins.
    expect(
      pairs(findRoute(1, 5, [heard(1, 2, 20), heard(2, 5, 20), ...longWay])),
    ).toEqual([
      [1, 2],
      [2, 5],
    ]);
  });

  it("counts the way travelled, and takes a way never heard only when nothing else connects", () => {
    // 1 → 2 was never heard; 2 → 1 was, 50 times.
    const oneWay = heard(1, 2, 0, 50);
    const around = [heard(1, 3, 10), heard(3, 2, 10)];
    expect(pairs(findRoute(1, 2, [oneWay, ...around]))).toEqual([
      [1, 3],
      [3, 2],
    ]);
    expect(pairs(findRoute(2, 1, [oneWay, ...around]))).toEqual([[1, 2]]);
    expect(pairs(findRoute(1, 2, [oneWay]))).toEqual([[1, 2]]);
  });

  // 1 – 2 – … – 10, nine links heard 100 times each: eight relays.
  const chain = Array.from({ length: 9 }, (_, index) =>
    heard(index + 1, index + 2, 100),
  );

  it("keeps to 7 hops, even when a longer route is likelier", () => {
    expect(findRoute(1, 9, chain)?.hops).toBe(7);
    expect(findRoute(1, 10, chain)).toBeNull();
    expect(
      pairs(findRoute(1, 10, [...chain, heard(1, 11, 1), heard(11, 10, 1)])),
    ).toEqual([
      [1, 11],
      [11, 10],
    ]);
  });

  it("spends no hop on a 0-hop relay after the first, which still needs a hop left", () => {
    const zeroHop = (from_node_id: number, to_node_id: number) => ({
      from_node_id,
      to_node_id,
      channel: link.channel,
      free: 3,
      costly: 0,
    });
    const route = findRoute(1, 10, chain, [zeroHop(3, 4)]);
    expect(route?.links).toHaveLength(9);
    expect(route?.hops).toBe(7);
    expect(findRoute(10, 1, chain, [zeroHop(3, 4)])).toBeNull();
    expect(findRoute(1, 10, chain, [zeroHop(1, 2)])).toBeNull();
    expect(findRoute(1, 10, chain, [zeroHop(8, 9)])).toBeNull();
  });
});
