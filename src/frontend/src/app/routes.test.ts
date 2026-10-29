import { describe, expect, it } from "vitest";

import {
  type MapLinkView,
  type NodeLinkView,
  type PacketLinkView,
  mapViewHref,
  nodeViewHref,
  packetViewHref,
  routeFromHash,
} from "./routes";
import {
  diagnosticsLinksHref,
  diagnosticsPacketsHref,
  relayChecksHref,
  surveyChecksHref,
} from "./nodeDisplay";

describe("routeFromHash", () => {
  it("reads node IDs as numbers or as !hex", () => {
    expect(routeFromHash("#nodes/!0000000a")).toEqual(
      routeFromHash("#nodes/10"),
    );
    expect(routeFromHash("#map/!FFFFFFFF")).toMatchObject({
      page: "map",
      nodeId: 0xffffffff,
    });
    expect(routeFromHash("#links/traceroutes/!00000001-2")).toMatchObject({
      pair: [1, 2],
    });
    expect(routeFromHash("#packets/7/hop/!00000001-!00000002")).toMatchObject({
      hop: { fromNodeId: 1, toNodeId: 2 },
    });
    // No node 0, and no number past 32 bits.
    for (const hash of ["#nodes/!00000000", "#nodes/!100000000"])
      expect(routeFromHash(hash), hash).toEqual({
        page: "map",
        nodeId: null,
        view: null,
      });
  });

  it("reads a shared map view, with the defaults for what it leaves out", () => {
    expect(
      routeFromHash("#map/!00000002?show=coverage,unknown&channel=Long%20Fast"),
    ).toEqual({
      page: "map",
      nodeId: 2,
      view: { show: ["coverage"], window: "3d", channel: "Long Fast" },
    });
    expect(routeFromHash("#map/2?window=2h")).toMatchObject({
      view: { show: [], window: "3d", channel: null },
    });
    // Only the node: the map as the viewer left it.
    expect(routeFromHash("#map/2")).toMatchObject({ view: null });
    expect(routeFromHash("#map/2?ref=chat")).toMatchObject({ view: null });
  });

  it("reads a shared node page", () => {
    expect(
      routeFromHash("#nodes/!00000002?map=expanded&show=gateways,coverage"),
    ).toEqual({
      page: "node",
      id: 2,
      view: { expanded: true, show: ["gateways", "coverage"] },
    });
    expect(routeFromHash("#nodes/2?show=traceroutes")).toMatchObject({
      view: { expanded: false, show: ["traceroutes"] },
    });
    expect(routeFromHash("#nodes/2")).toMatchObject({ view: null });
  });

  it("reads a shared packet page beside its pinned report", () => {
    expect(
      routeFromHash("#packets/7?reception=21&map=expanded&show=gateways"),
    ).toEqual({
      page: "packet",
      id: 7,
      hop: null,
      relays: false,
      receptionId: 21,
      view: { expanded: true, show: ["gateways"] },
    });
    expect(routeFromHash("#packets/7/hop/1-2?reception=21")).toMatchObject({
      hop: { fromNodeId: 1, toNodeId: 2 },
      receptionId: 21,
      view: null,
    });
    expect(routeFromHash("#packets/7?reception=0x15")).toMatchObject({
      receptionId: null,
    });
  });
});

describe("diagnostics tabs", () => {
  it("open each tab, with its node or reception, and the old links still work", () => {
    const tab = (hash: string) => routeFromHash(hash);
    expect(tab("#diagnostics")).toMatchObject({ tab: "overview" });
    expect(tab("#diagnostics/doubtful")).toMatchObject({ tab: "doubtful" });
    expect(tab(diagnosticsLinksHref(7))).toMatchObject({
      tab: "links",
      node: 7,
    });
    expect(tab(relayChecksHref())).toMatchObject({
      tab: "relays",
      receptionId: null,
    });
    expect(tab(relayChecksHref(21))).toMatchObject({
      tab: "relays",
      receptionId: 21,
      surveys: false,
    });
    expect(tab(surveyChecksHref())).toMatchObject({
      tab: "relays",
      receptionId: null,
      surveys: true,
      measurementId: null,
    });
    expect(tab(surveyChecksHref(9))).toMatchObject({
      tab: "relays",
      surveys: true,
      measurementId: 9,
    });
    expect(tab("#diagnostics/relays/surveys/0")).toEqual({
      page: "map",
      nodeId: null,
      view: null,
    });
    expect(tab(diagnosticsPacketsHref(null))).toMatchObject({
      tab: "packets",
      sender: null,
    });
    expect(tab("#diagnostics/packets/!00000002-3")).toMatchObject({
      page: "diagnostics",
      tab: "packets",
      sender: 2,
      heardDirectlyBy: 3,
    });
  });
});

describe("view links", () => {
  it("open the view they were built from", () => {
    const maps: MapLinkView[] = [
      {
        show: ["traceroute", "direct", "neighbor", "reach", "coverage"],
        window: "7d",
        channel: null,
      },
      { show: ["reach", "coverage"], window: "24h", channel: "A, B & C?" },
      { show: [], window: "all", channel: null },
    ];
    for (const view of maps)
      expect(routeFromHash(mapViewHref(0xa1b2c3d4, view))).toEqual({
        page: "map",
        nodeId: 0xa1b2c3d4,
        view,
      });
    const nodes: NodeLinkView[] = [
      { expanded: true, show: ["traceroutes", "gateways", "coverage"] },
      { expanded: false, show: ["coverage"] },
    ];
    for (const view of nodes)
      expect(routeFromHash(nodeViewHref(0xa1b2c3d4, view))).toEqual({
        page: "node",
        id: 0xa1b2c3d4,
        view,
      });
    const packets: PacketLinkView[] = [
      { expanded: true, show: ["relays", "gateways"] },
      { expanded: false, show: ["relays"] },
    ];
    for (const view of packets)
      expect(routeFromHash(packetViewHref(7, null, view))).toMatchObject({
        page: "packet",
        id: 7,
        hop: null,
        view,
      });
    const hop = { fromNodeId: 1, toNodeId: 0xa1b2c3d4 };
    expect(
      routeFromHash(packetViewHref(7, hop, packets[0], 1836014619324416)),
    ).toMatchObject({
      page: "packet",
      id: 7,
      hop,
      receptionId: 1836014619324416,
      view: packets[0],
    });
    // Nothing set: the plain packet link, the map as the viewer left it.
    expect(packetViewHref(7, null, { expanded: false, show: [] })).toBe(
      "#packets/7",
    );
  });
});
