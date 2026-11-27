import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TracerouteRoute } from "./api";
import { RouteHopList } from "./RouteItem";

afterEach(cleanup);

// A -> R -> B.
const [A, R, B] = [1, 2, 3];
const heardAt = "2026-09-27T09:30:00Z";
const route: TracerouteRoute = {
  packet_row_id: 7,
  heard_at: heardAt,
  is_return: false,
  hops: 2,
  km: null,
  reached: true,
  path: {
    nodes: [A, R, B],
    snr_db: [4, 6],
    mqtt: [false, false],
    reached: true,
  },
  path_hops: [
    { from_node_id: A, to_node_id: R, snr_db: 4 },
    { from_node_id: R, to_node_id: B, snr_db: 6 },
  ],
  hop: null,
  unlikely: false,
  hop_costs: [],
};

describe("A traceroute's hop list", () => {
  it("opens line of sight where both ends were when heard", () => {
    render(
      <RouteHopList
        route={route}
        nodeNames={new Map()}
        ends={{ requester: A, target: B }}
        placed={new Set([A, R])}
        spreadingFactor={11}
        at={
          new Map([
            [A, { latitude: 40, longitude: 2 }],
            [R, { latitude: 40.5, longitude: 2 }],
          ])
        }
        doubted={() => []}
        highlightedNode={null}
        onNodeHover={() => {}}
      />,
    );

    // B was not placed when heard, so R -> B offers none.
    expect(
      screen
        .getAllByRole("link", { name: "Line of sight" })
        .map((link) => link.getAttribute("href")),
    ).toEqual([`#links/line-of-sight/${A}-${R}@${Date.parse(heardAt)}`]);
  });
});
