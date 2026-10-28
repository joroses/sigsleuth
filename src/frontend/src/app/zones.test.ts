import { describe, expect, it } from "vitest";

import type { NodeSummary, Zone, ZonePair } from "./api";
import { mostSolidLink, textReachLookup } from "./zones";

function zone(zone_id: number): Zone {
  return {
    zone_id,
    name: `Zone ${zone_id}`,
    place: null,
    region: null,
    hub_node_id: zone_id,
    members: [zone_id],
    internal_links: 3,
    text_gateways: 2,
    text_senders: 1,
    text_count: 1,
    latitude: 40,
    longitude: -3,
    solid_paths: [],
    hull: [],
  };
}

function link(node_a: number, node_b: number) {
  return {
    node_a,
    node_b,
    observations: 5,
    weaker_margin_db: 8,
    km: null,
    estimated: false,
    solid: true,
    last_seen_at: "2026-09-25T08:00:00Z",
    spreading_factor: 11,
    a_to_b: {
      observations: 3,
      snr_samples: 3,
      average_snr: 2,
      average_rssi: null,
    },
    b_to_a: {
      observations: 2,
      snr_samples: 2,
      average_snr: -1,
      average_rssi: null,
    },
  };
}

function pair(zone_a: number, zone_b: number, fields: Partial<ZonePair> = {}) {
  return {
    zone_a,
    zone_b,
    observations: 10,
    strongest_margin_db: 12,
    hub_hops: 2,
    km: 20,
    through_nodes_a: 2,
    through_nodes_b: 2,
    solid_links: 2,
    way_round_links: null,
    way_round_zones: [],
    single_link: false,
    hanging_zones: [],
    warnings: [],
    sole_relay: null,
    links: [link(1, 2), link(3, 4)],
    ...fields,
  } satisfies ZonePair;
}

describe("textReachLookup", () => {
  it("tells a figure from a zone with no gateway and a zone with no sender", () => {
    const reach = {
      from_zone: 1,
      to_zone: 2,
      senders: 3,
      texts: 40,
      share: 0.94,
    };
    const lookup = textReachLookup({
      zones: [zone(1), zone(2), { ...zone(3), text_gateways: 0 }],
      text_reach: [reach],
    });

    expect(lookup(1, 2)).toBe(reach);
    expect(lookup(1, 3)).toBe("no gateway");
    expect(lookup(2, 1)).toBeNull();
  });
});

describe("mostSolidLink", () => {
  // Nodes 1-5 placed on the equator, node 9 without a position.
  const nodes = new Map(
    [1, 2, 3, 4, 5].map((id) => [
      id,
      { node_id: id, latitude: 0, longitude: id } as NodeSummary,
    ]),
  );
  const withMargin = (
    node_a: number,
    node_b: number,
    weaker_margin_db: number,
    solid = true,
  ) => ({ ...link(node_a, node_b), weaker_margin_db, solid });

  it("prefers a solid link to a stronger one that is not solid", () => {
    const best = mostSolidLink(
      pair(1, 2, {
        links: [withMargin(1, 2, 20, false), withMargin(3, 4, 6)],
      }),
      nodes,
    );
    expect(best?.link.node_a).toBe(3);
    expect(best?.b).toEqual([4, 0]);
  });

  it("takes the best margin among links whose nodes are placed", () => {
    const best = mostSolidLink(
      pair(1, 2, {
        links: [withMargin(1, 9, 15), withMargin(1, 2, 5), withMargin(3, 5, 9)],
      }),
      nodes,
    );
    expect(best?.link.node_b).toBe(5);
    expect(
      mostSolidLink(pair(1, 2, { links: [withMargin(1, 9, 15)] }), nodes),
    ).toBeNull();
  });
});
