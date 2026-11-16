import { describe, expect, it } from "vitest";

import { roundingCell } from "./roundingCell";

// The firmware's rounding of one coordinate in 1e-7 degrees.
function firmwareRounded(value: number, bits: number): number {
  return ((value & (-1 << (32 - bits))) + (1 << (31 - bits))) | 0;
}

describe("roundingCell", () => {
  it.each([
    [52.4589999, 16.9123456],
    [-33.8688197, -151.2092955],
  ])("is the firmware's grid cell around %f, %f", (latitude, longitude) => {
    const bits = 13;
    const lat = firmwareRounded(Math.round(latitude * 1e7), bits);
    const lon = firmwareRounded(Math.round(longitude * 1e7), bits);
    const cell = roundingCell(lat / 1e7, lon / 1e7, bits);
    const step = 2 ** (32 - bits);

    expect(cell.south).toBeLessThanOrEqual(latitude);
    expect(cell.north).toBeGreaterThan(latitude);
    expect(cell.west).toBeLessThanOrEqual(longitude);
    expect(cell.east).toBeGreaterThan(longitude);
    expect(Math.abs(Math.round(cell.south * 1e7) % step)).toBe(0);
    expect(Math.abs(Math.round(cell.west * 1e7) % step)).toBe(0);
  });
});
