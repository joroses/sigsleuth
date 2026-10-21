import { describe, expect, it } from "vitest";

import type { ContactLink, NodeContact } from "./api";
import { contactLayers } from "./nodeContacts";

const origin = { latitude: 50.0, longitude: 21.0 };

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
  it("tones a contact by its closest tie and draws lines only to linked ones", () => {
    const { marks, lines, unplaced } = contactLayers(
      [
        // A traceroute partner that also heard the node directly.
        contact(1, ["direct", "traceroute"], 0),
        contact(2, ["traceroute"], null),
        // A gateway that heard its packets after relays, with no link.
        contact(3, [], 5),
        contact(4, ["direct"], null, false),
      ],
      origin,
    );

    expect(marks.map((mark) => [mark.contact.node_id, mark.label])).toEqual([
      [1, "0"],
      [2, ""],
      [3, "5"],
    ]);
    expect(lines.features.map((line) => line.properties.radio)).toEqual([
      true,
      false,
    ]);
    expect(unplaced).toBe(1);
  });

  it("draws no lines without the node's own position", () => {
    const { marks, lines } = contactLayers([contact(1, ["direct"], 0)], null);

    expect(marks).toHaveLength(1);
    expect(lines.features).toEqual([]);
  });
});
