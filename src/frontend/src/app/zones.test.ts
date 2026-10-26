import { describe, expect, it } from "vitest";

import type { KeyRelay, NodeSummary, Zone, ZonePair } from "./api";
import { mostSolidLink, relayMark, textReachLookup, hullCentre } from "./zones";

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
    a_to_b: { observations: 3, snr_samples: 3, average_snr: 2 },
    b_to_a: { observations: 2, snr_samples: 2, average_snr: -1 },
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

describe("hullCentre", () => {
  it("is the middle of the area", () => {
    const [longitude, latitude] = hullCentre([
      [0, 40],
      [0.2, 40],
      [0.2, 40.2],
      [0, 40.2],
    ])!;

    expect(longitude).toBeCloseTo(0.1, 6);
    expect(latitude).toBeCloseTo(40.1, 6);
  });

  it("falls back to the mean of points on a line", () => {
    expect(
      hullCentre([
        [0, 40],
        [0.2, 40.2],
      ]),
    ).toEqual([0.1, 40.1]);
  });
});

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

describe("relayMark", () => {
  const relay = (fields: Partial<KeyRelay>): KeyRelay => ({
    node_id: 7,
    zone_id: 1,
    impact: 0,
    cuts_off: 0,
    no_solid_backup: 0,
    carries: 3,
    only_solid_link_to: [],
    neighbours: 4,
    last_seen_at: null,
    battery_level: null,
    battery_observed_at: null,
    firmware: null,
    ...fields,
  });

  const rules = {
    weak_margin_db: 4,
    long_route_hops: 4,
    key_relay_cut: 2,
  };

  it("marks a key relay with a warning even when it has no mast", () => {
    const now = Date.parse("2026-10-07T12:00:00Z");
    const heard = "2026-10-07T11:00:00Z";
    const late = "2026-10-07T06:00:00Z";

    expect(relayMark(relay({ last_seen_at: heard }), now, rules)).toBeNull();
    expect(relayMark(relay({ last_seen_at: late }), now, rules)).toBe(
      "warning",
    );
    expect(
      relayMark(relay({ last_seen_at: heard, battery_level: 15 }), now, rules),
    ).toBe("warning");
    // A mast keeps its own mark; the map adds the triangle beside it.
    expect(
      relayMark(
        relay({ last_seen_at: late, only_solid_link_to: [2] }),
        now,
        rules,
      ),
    ).toBe("filled");
    expect(
      relayMark(relay({ last_seen_at: late, impact: 3 }), now, rules),
    ).toBe("outlined");
  });
});
