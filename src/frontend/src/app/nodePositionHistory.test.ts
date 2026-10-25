import { describe, expect, it } from "vitest";

import type { NodePositionHistory } from "./api";
import { spacedPositions } from "./nodePositionHistory";

function positions(
  coordinates: [number, number][],
): NodePositionHistory["items"] {
  return coordinates.map(([latitude, longitude], index) => ({
    packet_row_id: coordinates.length - index,
    observed_at: new Date(Date.UTC(2026, 9, 25, 12, -index)).toISOString(),
    latitude,
    longitude,
    altitude_meters: null,
  }));
}

describe("spacedPositions", () => {
  it("keeps the newest report from a stationary cluster", () => {
    const items = positions([
      [52, 20],
      [52.001, 20.001],
      [52, 20],
    ]);

    expect(spacedPositions(items)).toEqual([items[0]]);
  });

  it("retains gradual travel once it reaches 3 km from the last shown point", () => {
    const items = positions([
      [0, 0],
      [0.01, 0],
      [0.02, 0],
      [0.03, 0],
      [0.04, 0],
      [0.06, 0],
    ]);

    expect(spacedPositions(items)).toEqual([items[0], items[3], items[5]]);
  });

  it("keeps a return visit in time order", () => {
    const items = positions([
      [52, 20],
      [52.1, 20],
      [52, 20],
    ]);

    expect(spacedPositions(items)).toEqual(items);
  });

  it("uses ground distance at high latitude and across the date line", () => {
    const polar = positions([
      [80, 20],
      [80, 20.1],
      [80, 20.2],
    ]);
    const crossing = positions([
      [0, 179.99],
      [0, -179.99],
      [0, -179.97],
    ]);

    expect(spacedPositions(polar)).toEqual([polar[0], polar[2]]);
    expect(spacedPositions(crossing)).toEqual([crossing[0], crossing[2]]);
  });
});
