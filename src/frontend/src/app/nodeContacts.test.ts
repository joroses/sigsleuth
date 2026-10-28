import { describe, expect, it } from "vitest";

import type { ContactLink, LinkRow, NodeContact } from "./api";
import {
  contactLayers,
  zeroCostLabel,
  zeroCostTitle,
  zeroCostWays,
} from "./nodeContacts";

const origin = { latitude: 50.0, longitude: 21.0 };
const NODE = 100;
const both = { traceroutes: true, gateways: true };

// A Links table row between the node and another, with the node as node_b.
function row(
  other: number,
  kind: LinkRow["link_kind"],
  channel = "LongFast",
): LinkRow {
  return {
    node_a: other,
    node_b: NODE,
    last_seen_at: "2026-10-11T05:40:00Z",
    channel,
    spreading_factor: 11,
    // What the node hears from the other.
    a_to_b: {
      observations: 3,
      snr_samples: 3,
      average_snr: 5,
      average_rssi: null,
    },
    b_to_a: {
      observations: 0,
      snr_samples: 0,
      average_snr: null,
      average_rssi: null,
    },
    link_kind: kind,
    distance_km: null,
    doubts: [],
  };
}

function contact(
  nodeId: number,
  links: ContactLink["kind"][],
  fewestHops: number | null,
  placed = true,
): NodeContact {
  return {
    node_id: nodeId,
    node: {
      node_id: nodeId,
      long_name: null,
      short_name: null,
      role: null,
      latitude: placed ? 50.5 : null,
      longitude: placed ? 21.5 : null,
      estimated: false,
    },
    links: links.map((kind) => ({
      kind,
      observations: 1,
      min_km: 3,
      max_km: 680,
      last_seen_at: "2026-10-11T05:40:00Z",
    })),
    heard_packets: fewestHops === null ? 0 : 4,
    fewest_hops: fewestHops,
    km: null,
  };
}

describe("contactLayers", () => {
  it("tones a contact by its closest tie and draws links only to placed ones", () => {
    const { marks, lines, unplaced } = contactLayers(
      NODE,
      [
        // A traceroute partner that also heard the node directly.
        contact(1, ["direct", "traceroute"], 0),
        contact(2, ["traceroute"], null),
        // A gateway that heard its packets after relays, with no link.
        contact(3, [], 5),
        contact(4, ["direct"], null, false),
      ],
      [
        row(1, "direct"),
        row(1, "traceroute"),
        row(2, "traceroute"),
        row(4, "direct"),
      ],
      [],
      origin,
      both,
    );

    expect(marks.map((mark) => [mark.contact.node_id, mark.label])).toEqual([
      [1, "0"],
      [2, ""],
      [3, "5"],
    ]);
    // Two halves a link; the node's half shows what it hears.
    expect(lines.direct.features.map((half) => half.properties.tier)).toEqual([
      "good",
      "unknown",
    ]);
    expect(
      lines.traceroute.features.map((half) => half.properties.nodeB),
    ).toEqual([1, 1, 2, 2]);
    expect(lines.neighbor.features).toEqual([]);
    expect(unplaced).toBe(1);
  });

  it("draws no lines without the node's own position", () => {
    const { marks, lines } = contactLayers(
      NODE,
      [contact(1, ["direct"], 0)],
      [row(1, "direct")],
      [],
      null,
      both,
    );

    expect(marks).toHaveLength(1);
    expect(lines.direct.features).toEqual([]);
  });
});

describe("0-cost hops", () => {
  const free = (from: number, to: number, channel = "LongFast") => ({
    from_node_id: from,
    to_node_id: to,
    channel,
    free: 3,
    costly: 1,
  });

  it("marks the traceroute line on the hop's channel only", () => {
    const { zeroCost } = contactLayers(
      NODE,
      [
        contact(1, ["direct", "traceroute"], 0),
        contact(2, ["traceroute"], null),
      ],
      [
        row(1, "direct"),
        row(1, "traceroute"),
        row(1, "traceroute", "MediumFast"),
        row(2, "traceroute"),
      ],
      [free(1, NODE), free(NODE, 2, "MediumFast")],
      origin,
      both,
    );

    expect(zeroCost.features).toHaveLength(1);
    // At the line's middle, between the node and its partner.
    const [, latitude] = zeroCost.features[0].geometry.coordinates;
    expect(latitude).toBeGreaterThan(origin.latitude);
    expect(latitude).toBeLessThan(50.5);
  });

  it("draws no arrow without both positions and a visible traceroute line", () => {
    const arrows = (
      placed: boolean,
      position: typeof origin | null,
      traceroutes: boolean,
    ) =>
      contactLayers(
        NODE,
        [contact(1, ["direct", "traceroute"], 0, placed)],
        [row(1, "direct"), row(1, "traceroute")],
        [free(NODE, 1)],
        position,
        { traceroutes, gateways: true },
      ).zeroCost.features;

    expect(arrows(false, origin, true)).toEqual([]);
    expect(arrows(true, null, true)).toEqual([]);
    expect(arrows(true, origin, false)).toEqual([]);
  });

  it("points the arrowhead the free way, two back to back both ways", () => {
    // Partners due north of the node: east is 0°, clockwise on screen.
    const north = (nodeId: number): NodeContact => {
      const found = contact(nodeId, ["traceroute"], null);
      return {
        ...found,
        node: { ...found.node!, latitude: 51, longitude: 21 },
      };
    };
    const arrows = (hops: ReturnType<typeof free>[]) =>
      contactLayers(
        NODE,
        [north(1)],
        [row(1, "traceroute")],
        hops,
        origin,
        both,
      ).zeroCost.features.map(({ properties: { rotation, both } }) => ({
        rotation,
        both,
      }));

    // Out: the partner passes the node's copies on for free.
    expect(arrows([free(NODE, 1)])).toEqual([{ rotation: -90, both: false }]);
    expect(arrows([free(1, NODE)])).toEqual([{ rotation: 90, both: false }]);
    expect(arrows([free(1, NODE), free(NODE, 1)])).toEqual([
      { rotation: -90, both: true },
      { rotation: 90, both: true },
    ]);
  });

  it("keeps which of the two relays the other's copies for free", () => {
    const ways = zeroCostWays(NODE, [
      free(NODE, 1),
      free(1, NODE),
      free(NODE, 2),
    ]);

    expect(ways.get("1:LongFast")).toEqual({
      out: free(NODE, 1),
      into: free(1, NODE),
    });
    expect(ways.get("2:LongFast")).toEqual({ out: free(NODE, 2), into: null });
    expect(zeroCostTitle(ways.get("2:LongFast")!)).toBe("3 of 4 traceroutes");
    expect(zeroCostTitle(ways.get("1:LongFast")!)).toBe(
      "Out 3 of 4 traceroutes, in 3 of 4",
    );
    expect(zeroCostLabel(ways.get("1:LongFast")!)).toBe("0-hop both ways");
    expect(zeroCostLabel(ways.get("2:LongFast")!)).toBe("0-hop out");
  });
});
