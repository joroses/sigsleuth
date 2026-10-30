import { describe, expect, it } from "vitest";

import type { MapNode } from "./api";
import {
  ANY,
  busiestChannel,
  filterCounts,
  linkableNodes,
  NOT_REPORTED,
  onChosenChannel,
  matchesNodeFilter,
  shownNodes,
} from "./mapNodeFilter";

const node: MapNode = {
  node_id: 1,
  long_name: null,
  short_name: null,
  role: "CLIENT",
  hardware_model: null,
  last_sent_at: "2026-09-25T12:00:00Z",
  last_sent_packet_row_id: 1,
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
const gateway = { ...node, last_gateway_at: "2026-09-25T12:00:00Z" };
const router = { ...gateway, role: "ROUTER" };
const everyCategory = { relay: true, gateway: true, other: true };

describe("matchesNodeFilter", () => {
  it("hides the categories switched off", () => {
    const noGateways = {
      categories: { ...everyCategory, gateway: false },
      role: ANY,
      channel: ANY,
      estimated: true,
    };
    expect(matchesNodeFilter(gateway, noGateways)).toBe(false);
    // A router that is also a gateway is a relay.
    expect(matchesNodeFilter(router, noGateways)).toBe(true);
    expect(matchesNodeFilter(node, noGateways)).toBe(true);
  });

  it("needs the category, role and channel to all match", () => {
    const filter = {
      categories: everyCategory,
      role: "CLIENT",
      channel: "LongFast",
      estimated: true,
    };
    expect(matchesNodeFilter(gateway, filter)).toBe(true);
    expect(matchesNodeFilter(router, filter)).toBe(false);
    expect(
      matchesNodeFilter({ ...gateway, channel: "MediumFast" }, filter),
    ).toBe(false);
  });

  it("hides nodes drawn at an estimate when estimates are off", () => {
    const estimated = {
      ...node,
      position: null,
      estimate: {
        node_id: 1,
        latitude: 40.4,
        longitude: -3.7,
        neighbours: 3,
        spread_km: 5,
        from_traceroute: false,
      },
    };
    const filter = {
      categories: everyCategory,
      role: ANY,
      channel: ANY,
      estimated: false,
    };
    expect(matchesNodeFilter(estimated, filter)).toBe(false);
    expect(matchesNodeFilter(node, filter)).toBe(true);
    expect(filterCounts([node, estimated], filter).estimated).toBe(1);
  });
});

describe("linkableNodes and shownNodes", () => {
  const onMesh = { ...node, node_id: 1, channel: "MediumFast" };
  // A relay that has since moved to another preset.
  const movedRelay = { ...router, node_id: 2, channel: "ShortFast" };
  const elsewhere = { ...node, node_id: 3, channel: "LongFast" };
  const nodes = [onMesh, movedRelay, elsewhere];
  const mediumFast = {
    categories: everyCategory,
    role: ANY,
    channel: "MediumFast",
    estimated: true,
  };
  const ids = (shown: MapNode[]) => shown.map((each) => each.node_id);

  it("lets links end at a node whose latest channel is another", () => {
    expect(ids(linkableNodes(nodes, mediumFast, null))).toEqual([1, 2, 3]);
  });

  it("still keeps links from nodes the other filters hide", () => {
    const noRelays = {
      ...mediumFast,
      categories: { ...everyCategory, relay: false },
    };
    expect(ids(linkableNodes(nodes, noRelays, null))).toEqual([1, 3]);
    expect(ids(linkableNodes(nodes, noRelays, 2))).toEqual([1, 2, 3]);
  });

  it("keeps links to nodes with no channel when Unknown is chosen", () => {
    const silent = { ...node, node_id: 4, channel: null };
    const unknown = { ...mediumFast, channel: NOT_REPORTED };
    const all = [...nodes, silent];
    expect(ids(linkableNodes(all, unknown, null))).toEqual([4]);
    expect(ids(shownNodes(all, unknown, null, new Set([1, 2, 4])))).toEqual([
      4,
    ]);
  });

  it("draws the ends of shown links besides the nodes on the channel", () => {
    expect(ids(shownNodes(nodes, mediumFast, null, new Set()))).toEqual([1]);
    expect(ids(shownNodes(nodes, mediumFast, null, new Set([1, 2])))).toEqual([
      1, 2,
    ]);
  });
});

describe("filterCounts", () => {
  const nodes = [node, gateway, router, { ...node, channel: "MediumFast" }];

  it("counts each filter's choices with only the other filters applied", () => {
    const counts = filterCounts(nodes, {
      categories: { ...everyCategory, other: false },
      role: "CLIENT",
      channel: ANY,
      estimated: true,
    });
    // The switched-off category still counts its clients.
    expect(counts.categories).toEqual({ relay: 0, gateway: 1, other: 2 });
    // Roles ignore the role choice but not the hidden category.
    expect(counts.roles.map((role) => [role.value, role.count])).toEqual([
      ["CLIENT", 1],
      ["ROUTER", 1],
    ]);
    expect(counts.channels.map((channel) => channel.count)).toEqual([1, 0]);
  });

  it("keeps values with no matching nodes, in their overall order", () => {
    const counts = filterCounts(nodes, {
      categories: everyCategory,
      role: ANY,
      channel: "MediumFast",
      estimated: true,
    });
    expect(counts.roles.map((role) => [role.value, role.count])).toEqual([
      ["CLIENT", 1],
      ["ROUTER", 0],
    ]);
  });
});

describe("busiestChannel", () => {
  it("picks the channel most nodes reported, never Unknown", () => {
    const silent = { ...node, channel: null };
    expect(
      busiestChannel([
        silent,
        silent,
        silent,
        node,
        node,
        { ...node, channel: "MediumFast" },
      ]),
    ).toBe("LongFast");
    expect(busiestChannel([silent])).toBeNull();
  });
});

describe("onChosenChannel", () => {
  it("keeps only Unknown links for Unknown and every channel for Any", () => {
    const links = ["Unknown", "LongFast", "MediumFast"].map((channel) => ({
      channel,
    }));
    expect(links.filter((link) => onChosenChannel(link, NOT_REPORTED))).toEqual(
      [{ channel: "Unknown" }],
    );
    expect(links.filter((link) => onChosenChannel(link, ANY))).toEqual(links);
    expect(links.filter((link) => onChosenChannel(link, "MediumFast"))).toEqual(
      [{ channel: "MediumFast" }],
    );
  });
});
