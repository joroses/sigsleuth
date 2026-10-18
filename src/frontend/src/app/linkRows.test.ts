import { describe, expect, it } from "vitest";

import type { LinkRow } from "./api";
import { fromNode, sortLinks } from "./linkRows";

function link(nodeB: number, seen: number, km: number | null): LinkRow {
  return {
    node_a: 1,
    node_b: nodeB,
    last_seen_at: "2026-10-18T10:00:00Z",
    channel: "MediumFast",
    spreading_factor: 9,
    a_to_b: { observations: seen, snr_samples: 0, average_snr: null },
    b_to_a: { observations: 1, snr_samples: 1, average_snr: -3 },
    link_kind: "direct",
    distance_km: km,
    unlikely: false,
    pending: false,
  };
}

describe("a node page's links", () => {
  it("put the node's own figures first", () => {
    const heard = link(5, 7, null);

    expect(fromNode(heard, 1)).toBe(heard);
    expect(fromNode(heard, 5)).toMatchObject({
      node_a: 5,
      node_b: 1,
      a_to_b: heard.b_to_a,
      b_to_a: heard.a_to_b,
    });
  });

  it("rank a link to a node with no position by how often it was heard", () => {
    const links = [link(2, 3, 12.5), link(3, 40, null), link(4, 9, 80)];
    const none = new Set<number>();

    const byObservations = sortLinks(links, "observations", false, none, none);
    const byDistance = sortLinks(links, "distance", false, none, none);

    expect(byObservations.map((item) => item.node_b)).toEqual([3, 4, 2]);
    expect(byDistance.map((item) => item.node_b)).toEqual([4, 2, 3]);
  });

  it("rank by last seen, latest first or oldest first", () => {
    const at = (nodeB: number, time: string) => ({
      ...link(nodeB, 1, null),
      last_seen_at: time,
    });
    const links = [
      at(2, "2026-10-17T08:00:00Z"),
      at(3, "2026-10-18T09:30:00Z"),
      at(4, "2026-10-12T23:00:00Z"),
    ];
    const none = new Set<number>();

    const latest = sortLinks(links, "last_seen", false, none, none);
    const oldest = sortLinks(links, "last_seen", true, none, none);

    expect(latest.map((item) => item.node_b)).toEqual([3, 2, 4]);
    expect(oldest.map((item) => item.node_b)).toEqual([4, 2, 3]);
  });
});
