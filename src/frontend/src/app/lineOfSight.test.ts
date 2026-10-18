import { describe, expect, it } from "vitest";

import {
  TILE_SIZE,
  buildProfile,
  curvatureRise,
  decodeTerrarium,
  decodeTerrariumImage,
  distanceMeters,
  endHeight,
  findSite,
  fresnelRadius,
  groundHeight,
  needsSite,
  planPath,
  positionRoundingKm,
  samplePath,
  SITE_ZOOM,
  siteRadiusKm,
  siteTiles,
  tierRuns,
  tileId,
  worldPixel,
  type HeightTile,
  type SiteSearch,
} from "./lineOfSight";

// About 10 km apart east–west near Madrid.
const A = { latitude: 40.4, longitude: -3.7 };
const B = { latitude: 40.4, longitude: -3.58176 };

describe("path", () => {
  it("measures great-circle distance", () => {
    expect(distanceMeters(A, B) / 1000).toBeCloseTo(10.0, 1);
    // One degree of latitude is about 111.2 km.
    expect(
      distanceMeters(
        { latitude: 0, longitude: 0 },
        { latitude: 1, longitude: 0 },
      ) / 1000,
    ).toBeCloseTo(111.2, 1);
  });

  it("samples both ends and evenly between them", () => {
    const points = samplePath(A, B, 5);
    expect(points).toHaveLength(5);
    expect(points[0].latitude).toBeCloseTo(A.latitude, 9);
    expect(points[4].longitude).toBeCloseTo(B.longitude, 9);
    expect(points[2].distance).toBeCloseTo(points[4].distance / 2, 6);
  });

  it("uses zoom 12 for a short path, about one sample a pixel", () => {
    const plan = planPath(A, B);
    expect(plan.zoom).toBe(12);
    // 10 km at 38 m × cos(40.4°) a pixel is about 340 pixels.
    expect(plan.points.length).toBeGreaterThan(300);
    expect(plan.points.length).toBeLessThan(380);
  });

  it("lowers the zoom so a long path fetches few tiles", () => {
    // About 80 km east–west: 25 m zoom 12 pixels would be sampled only every
    // about 80 m, so zoom 10 pixels (about 117 m here) are used instead.
    const plan = planPath(A, { latitude: 40.4, longitude: -2.754 });
    expect(plan.zoom).toBe(10);
    expect(plan.points.length).toBeGreaterThan(600);
    expect(plan.points.length).toBeLessThanOrEqual(1000);
    expect(plan.tiles.length).toBeLessThanOrEqual(8);
  });

  it("keeps at least 100 samples on a very short path", () => {
    const near = { latitude: A.latitude, longitude: A.longitude + 0.001 };
    expect(planPath(A, near).points.length).toBeGreaterThanOrEqual(100);
  });
});

describe("terrain", () => {
  it("decodes Terrarium colours to metres", () => {
    expect(decodeTerrarium(128, 0, 0)).toBe(0);
    expect(decodeTerrarium(128, 100, 128)).toBe(100.5);
    expect(decodeTerrarium(127, 255, 0)).toBe(-1);
  });

  it("reads a flat tile as the same height everywhere", () => {
    const image = new Uint8ClampedArray(TILE_SIZE * TILE_SIZE * 4);
    for (let index = 0; index < image.length; index += 4) {
      image.set([128, 100, 0, 255], index);
    }
    const flat = decodeTerrariumImage(image);
    const plan = planPath(A, B);
    const tiles = new Map(plan.tiles.map((tile) => [tileId(tile), flat]));
    for (const point of plan.points) {
      expect(groundHeight(point, plan.zoom, tiles)).toBeCloseTo(100, 6);
    }
  });

  it("has no height where a tile is missing", () => {
    const plan = planPath(A, B);
    expect(groundHeight(plan.points[0], plan.zoom, new Map())).toBeNull();
  });
});

describe("radio geometry", () => {
  it("raises terrain by the Earth's curve", () => {
    expect(curvatureRise(200_000, 200_000)).toBeCloseTo(2354, 0);
    expect(curvatureRise(5_000, 5_000)).toBeCloseTo(1.47, 2);
    expect(curvatureRise(0, 10_000)).toBe(0);
  });

  it("sizes the Fresnel zone at 890 MHz", () => {
    expect(fresnelRadius(5_000, 5_000)).toBeCloseTo(29.0, 1);
    expect(fresnelRadius(0, 10_000)).toBe(0);
  });
});

describe("endHeight", () => {
  it("uses a reported altitude only within 300 m above the ground", () => {
    expect(endHeight(1000, 500)).toEqual({
      ground: 1000,
      height: 1002,
      source: "ground",
      ignored: 500,
      search: null,
    });
    expect(endHeight(1000, 1400).height).toBe(1002);
    expect(endHeight(1000, 1400).ignored).toBe(1400);
    expect(endHeight(1000, 1200).height).toBe(1200);
    expect(endHeight(1000, 0).height).toBe(1002);
    expect(endHeight(1000, null)).toEqual({
      ground: 1000,
      height: 1002,
      source: "ground",
      ignored: null,
      search: null,
    });
    expect(endHeight(1000, 1030)).toEqual({
      ground: 1000,
      height: 1030,
      source: "reported",
      ignored: null,
      search: null,
    });
  });

  it("looks for a site only for an altitude too high for the ground", () => {
    expect(needsSite(endHeight(487, 800))).toBe(true);
    expect(needsSite(endHeight(487, 400))).toBe(false);
    expect(needsSite(endHeight(487, 700))).toBe(false);
    expect(needsSite(endHeight(487, null))).toBe(false);
  });

  it("starts at the reported altitude on a site, never below its ground", () => {
    const search: SiteSearch = {
      from: A,
      fromGround: 487,
      roundingKm: 3.7,
      radiusKm: 4.2,
      site: { ...B, offset: 1000, ground: 760 },
    };
    expect(endHeight(781, 800, search)).toMatchObject({
      height: 800,
      source: "site",
      ignored: null,
    });
    expect(endHeight(820, 800, search).height).toBe(822);
    expect(endHeight(487, 800, { ...search, site: null })).toMatchObject({
      height: 489,
      source: "ground",
      ignored: 800,
    });
  });
});

describe("site search", () => {
  // EA7-CA-05's position, rounded by its firmware to 13 bits.
  const rounded = { latitude: 36.8836608, longitude: -5.4263808 };

  it("reads a position's deliberate rounding from its coordinates", () => {
    expect(positionRoundingKm(rounded)).toBeCloseTo(3.73, 1);
    expect(siteRadiusKm(rounded)).toBeCloseTo(4.23, 1);
    expect(
      positionRoundingKm({ latitude: 36.8836123, longitude: -5.4263871 }),
    ).toBe(0);
    expect(siteRadiusKm({ latitude: 36.8836123, longitude: -5.4263871 })).toBe(
      3,
    );
  });

  // Flat ground at 487 m with one high pixel about `km` north of the position.
  function terrain(km: number, height: number) {
    const tiles = new Map<string, HeightTile>(
      siteTiles(rounded, 4.2).map((tile) => [
        tileId(tile),
        new Float32Array(TILE_SIZE * TILE_SIZE).fill(487),
      ]),
    );
    const [column, row] = worldPixel(
      { latitude: rounded.latitude + km / 111.2, longitude: rounded.longitude },
      SITE_ZOOM,
    ).map(Math.floor);
    const tile = {
      z: SITE_ZOOM,
      x: Math.floor(column / TILE_SIZE),
      y: Math.floor(row / TILE_SIZE),
    };
    tiles.get(tileId(tile))![
      (row % TILE_SIZE) * TILE_SIZE + (column % TILE_SIZE)
    ] = height;
    return tiles;
  }

  it("finds the nearest ground reaching the altitude within the radius", () => {
    const site = findSite(rounded, 750, 4.2, terrain(1, 790));
    expect(site?.ground).toBe(790);
    expect(site?.offset).toBeGreaterThan(950);
    expect(site?.offset).toBeLessThan(1050);
  });

  it("finds no site when the ground is too low or too far", () => {
    expect(findSite(rounded, 750, 4.2, terrain(1, 700))).toBeNull();
    expect(findSite(rounded, 750, 0.5, terrain(1, 790))).toBeNull();
  });

  it("gives no answer without its tiles", () => {
    expect(findSite(rounded, 750, 4.2, new Map())).toBeUndefined();
  });
});

describe("buildProfile", () => {
  const points = samplePath(A, B, 101);
  const flat = (height: number) => points.map(() => height);

  it("is clear over flat ground with nodes 30 m up", () => {
    const profile = buildProfile(points, flat(100), 130, 130);
    expect(profile.worst?.tier).toBe("clear");
    // Worst at the middle: 30 m less 1.5 m of curve, against 29 m of zone.
    expect(profile.worst?.distance).toBeCloseTo(profile.distance / 2, 0);
    expect(profile.worst?.clearance).toBeCloseTo(28.5, 0);
    expect(tierRuns(profile.samples)).toHaveLength(1);
  });

  it("gives the sight line's true altitude, sagging by the curve", () => {
    const profile = buildProfile(points, flat(100), 130, 130);
    const middle = profile.samples[50];
    expect(middle.ground).toBe(100);
    expect(middle.sightLine).toBe(130);
    expect(middle.sightAltitude).toBeCloseTo(128.5, 0);
    expect(middle.sightAltitude! - middle.ground!).toBeCloseTo(
      middle.clearance!,
    );
  });

  it("is partly obstructed when the zone grazes the ground", () => {
    const profile = buildProfile(points, flat(100), 110, 110);
    expect(profile.worst?.tier).toBe("partial");
  });

  it("is not blocked by the Earth's curve alone without altitudes", () => {
    const profile = buildProfile(points, flat(100), null, null);
    expect(profile.worst?.tier).toBe("partial");
  });

  it("is blocked by a ridge in the middle", () => {
    const grounds = flat(100);
    grounds[50] = 300;
    const profile = buildProfile(points, grounds, 130, 130);
    expect(profile.worst?.tier).toBe("blocked");
    expect(profile.worst?.distance).toBeCloseTo(profile.distance / 2, 0);
    const runs = tierRuns(profile.samples);
    expect(runs.map((run) => run.tier)).toContain("blocked");
    // Runs join: each ends on the sample the next one starts with.
    expect(runs[0].samples.at(-1)).toBe(runs[1].samples[0]);
  });

  it("keeps a verdict when the ends stand at very different heights", () => {
    // 1000 + (100.2 - 1000) * d / d rounds to just under 100.2.
    const grounds = flat(100);
    grounds[0] = 1000;
    grounds[grounds.length - 1] = 100.2;
    for (const ground of [grounds, grounds.toReversed()]) {
      const profile = buildProfile(points, ground, null, null);
      expect(Number.isFinite(profile.worst?.ratio)).toBe(true);
    }
  });

  it("marks missing terrain as a gap rather than failing", () => {
    const grounds: (number | null)[] = flat(100);
    grounds.fill(null, 20, 30);
    const profile = buildProfile(points, grounds, 130, 130);
    expect(profile.incomplete).toBe(true);
    expect(profile.worst?.tier).toBe("clear");
    expect(tierRuns(profile.samples)).toHaveLength(2);
  });
});
