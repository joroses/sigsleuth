import { describe, expect, it } from "vitest";

import type { ContactLink, LinkRow, NodeContact } from "./api";
import { contactLayers } from "./nodeContacts";

const origin = { latitude: 50.0, longitude: 21.0 };
const NODE = 100;
const both = { traceroutes: true, gateways: true };

// A Links table row between the node and another, with the node as node_b.
function row(other: number, kind: LinkRow["link_kind"]): LinkRow {
  return {
    node_a: other,
    node_b: NODE,
    last_seen_at: "2026-10-11T05:40:00Z",
    channel: "LongFast",
    spreading_factor: 11,
    // What the node hears from the other.
    a_to_b: { observations: 3, snr_samples: 3, average_snr: 5 },
    b_to_a: { observations: 0, snr_samples: 0, average_snr: null },
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
      null,
      both,
    );

    expect(marks).toHaveLength(1);
    expect(lines.direct.features).toEqual([]);
  });
});
