import { describe, expect, it } from "vitest";

import {
  type MapLinkView,
  type NodeLinkView,
  mapViewHref,
  nodeViewHref,
  routeFromHash,
} from "./routes";

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
  });
});
