import { describe, expect, it } from "vitest";

import type { CoverageHeardNode, MapNode } from "./api";
import {
  buildCoverageLines,
  cellBounds,
  cellsInView,
  marginColor,
  nextCoverageArea,
} from "./coverageLayer";

const heard = (
  node_id: number,
  how: CoverageHeardNode["how"],
  candidates = 1,
): CoverageHeardNode => ({
  node_id,
  long_name: null,
  short_name: null,
  how,
  candidates,
  measurements: 1,
  guessed: 0,
  average_snr: 10,
  average_margin: null,
  channel: null,
  last_heard_at: "2026-09-25T09:00:00Z",
});

describe("coverage cells", () => {
  it("draws the fine cell the server put a position in, about 230 m across", () => {
    // The backend's cell for 41.91974 N, 1.569995 E.
    const [west, south, east, north] = cellBounds({
      level: 17,
      x: 66107,
      y: 48695,
    });
    expect(west).toBeLessThan(1.569995);
    expect(east).toBeGreaterThan(1.569995);
    expect(south).toBeLessThan(41.91974);
    expect(north).toBeGreaterThan(41.91974);
    const widthM = (east - west) * 111_320 * Math.cos((41.92 * Math.PI) / 180);
    expect(widthM).toBeGreaterThan(220);
    expect(widthM).toBeLessThan(235);
  });

  it("asks again only when the map leaves the area fetched or its level", () => {
    const view = { west: 0, south: 40, east: 2, north: 41, zoom: 8.2 };
    const area = nextCoverageArea(null, view);
    expect(area.fetched).toEqual({
      west: -1,
      south: 39.5,
      east: 3,
      north: 41.5,
      zoom: 8.2,
    });
    const panned = {
      west: 0.8,
      south: 40.3,
      east: 2.8,
      north: 41.3,
      zoom: 8.7,
    };
    const moved = nextCoverageArea(area, panned);
    expect(moved.fetched).toBe(area.fetched);
    expect(moved.view).toBe(panned);
    expect(nextCoverageArea(area, { ...panned, zoom: 9 }).fetched).not.toBe(
      area.fetched,
    );
    const outside = { ...panned, west: 1.5, east: 3.5 };
    expect(nextCoverageArea(area, outside).fetched.east).toBe(4.5);
  });

  it("counts only the cells in view", () => {
    const cell = (x: number) => ({
      x,
      y: 48695,
      measurements: 1,
      average_margin: null,
      best_margin: null,
      nodes: 1,
      guessed: 0,
    });
    // Cell 66107 holds 1.569995 E; 66200 is about 25 km east of it.
    const list = {
      level: 17,
      truncated: false,
      cells: [cell(66107), cell(66200)],
    };
    const view = {
      west: 1.56,
      south: 41.9,
      east: 1.58,
      north: 41.94,
      zoom: 15,
    };
    expect(cellsInView(list, view).cells.map((c) => c.x)).toEqual([66107]);
  });

  it("draws solid lines to certain nodes, dashed to guesses, none to undrawn nodes", () => {
    const drawn = new Map(
      [1, 2].map((node_id) => [
        node_id,
        { node_id, latitude: 42, longitude: node_id } as MapNode,
      ]),
    );
    const lines = buildCoverageLines(
      [1.5, 41.9],
      [heard(1, "survey_link", 3), heard(2, "reported"), heard(9, "direct")],
      drawn,
    );
    expect(
      lines.features.map((line) => [
        line.properties.nodeId,
        line.properties.certain,
      ]),
    ).toEqual([
      [1, true],
      [2, false],
    ]);
  });
});

describe("margin colours", () => {
  it("follow the cells' scale, clamped at its ends", () => {
    expect(marginColor(-3)).toBe("#d0453a");
    expect(marginColor(6)).toBe("#e0a526");
    expect(marginColor(3)).toBe("#d87530");
    expect(marginColor(40)).toBe("#1f7a3f");
    expect(marginColor(null)).toBe("#9aa39e");
  });
});
