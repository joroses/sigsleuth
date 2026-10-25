import { describe, expect, it } from "vitest";

import { distanceToPath } from "./lineHit";

describe("distanceToPath", () => {
  const path = [
    { x: 0, y: 0 },
    { x: 10, y: 0 },
    { x: 10, y: 10 },
  ];

  it("measures across to the nearest segment", () => {
    expect(distanceToPath({ x: 5, y: 3 }, path)).toBe(3);
    expect(distanceToPath({ x: 14, y: 6 }, path)).toBe(4);
  });

  it("measures to an end past the path", () => {
    expect(distanceToPath({ x: -3, y: -4 }, path)).toBe(5);
  });

  it("handles a single point and a zero-length segment", () => {
    expect(distanceToPath({ x: 3, y: 4 }, [{ x: 0, y: 0 }])).toBe(5);
    expect(
      distanceToPath({ x: 3, y: 4 }, [
        { x: 0, y: 0 },
        { x: 0, y: 0 },
      ]),
    ).toBe(5);
  });
});
