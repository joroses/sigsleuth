// Line of sight between two nodes over a terrain profile. An estimate: node
// positions may be rounded or wrong, GPS altitude is rough, and the terrain
// model has no buildings and only some trees. This page alone uses the
// profile, so calculations stay here beside browser-fetched terrain tiles.

/** One frequency for every band; 868 and 915 MHz differ by about 3% in Fresnel radius. */
export const FREQUENCY_HZ = 890e6;
export const WAVELENGTH_M = 299_792_458 / FREQUENCY_HZ;
export const EARTH_RADIUS_M = 6_371_000;
/** The usual effective Earth radius factor, allowing for atmospheric bending. */
export const EARTH_RADIUS_FACTOR = 4 / 3;
/**
 * A reported altitude further above the ground than this needs nearby ground
 * that high (a site), else it is ignored.
 */
export const MAX_ALTITUDE_ABOVE_GROUND_M = 300;
/**
 * A site is the ground nearest the node's position reaching within
 * SITE_SLACK_M of its reported altitude, within SITE_MIN_KM or the position's
 * rounding, whichever is larger. Even a position that looks exact can be
 * kilometres off its summit.
 */
export const SITE_MIN_KM = 3;
export const SITE_MAX_KM = 15;
export const SITE_SLACK_M = 50;
/** About 60 m a pixel: the path's own zoom drops to 8 on long paths, too coarse for summits. */
export const SITE_ZOOM = 11;
/**
 * The antenna height above the ground assumed when no usable altitude is
 * reported. At 0 m the Earth's curve alone blocks every flat path. 14% of live
 * loranet.pl nodes fell back to the ground on 2026-10-09; revisit if most
 * fallbacks turn out to be masts.
 */
export const ASSUMED_ANTENNA_HEIGHT_M = 2;
/** Clearance, as a share of the Fresnel radius, from which a path counts as clear. */
export const CLEAR_RATIO = 0.6;

export const TILE_SIZE = 256;
const MIN_ZOOM = 8;
const MAX_ZOOM = 12;
const MIN_SAMPLES = 100;
const MAX_SAMPLES = 1000;

export interface LatLon {
  latitude: number;
  longitude: number;
}

export interface PathPoint extends LatLon {
  /** Metres from A along the path. */
  distance: number;
}

export interface TileKey {
  z: number;
  x: number;
  y: number;
}

/** Heights in metres, row by row, TILE_SIZE × TILE_SIZE; null when the tile failed. */
export type HeightTile = Float32Array | null;

export type ClearanceTier = "clear" | "partial" | "blocked";

export const TIER_LABELS: Record<ClearanceTier, string> = {
  clear: "Clear",
  partial: "Fresnel zone partly obstructed",
  blocked: "Blocked",
};

/** The map's link colours: good, fair and marginal. */
export const TIER_COLORS: Record<ClearanceTier, string> = {
  clear: "#2e9e4f",
  partial: "#e0a526",
  blocked: "#d0453a",
};

/** A distance given in metres, shown in km with its unit. */
export function formatDistance(meters: number): string {
  const km = meters / 1000;
  return `${km.toLocaleString(undefined, { maximumFractionDigits: km < 10 ? 2 : 1 })} km`;
}

export function formatMeters(meters: number | null): string {
  return meters === null ? "–" : `${Math.round(meters).toLocaleString()} m`;
}

/** A clearance ratio as a percentage of the Fresnel radius. */
export function formatPercent(ratio: number | null): string {
  return ratio === null || !Number.isFinite(ratio)
    ? "–"
    : `${Math.round(ratio * 100)}%`;
}

const radians = (degrees: number) => (degrees * Math.PI) / 180;
const degrees = (radians: number) => (radians * 180) / Math.PI;

/** Great-circle distance in metres (haversine). */
export function distanceMeters(a: LatLon, b: LatLon): number {
  const dLat = radians(b.latitude - a.latitude);
  const dLon = radians(b.longitude - a.longitude);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(radians(a.latitude)) *
      Math.cos(radians(b.latitude)) *
      Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** `count` evenly spaced points on the great circle from a to b, both ends included. */
export function samplePath(a: LatLon, b: LatLon, count: number): PathPoint[] {
  const total = distanceMeters(a, b);
  const angle = total / EARTH_RADIUS_M;
  const toVector = ({ latitude, longitude }: LatLon) => [
    Math.cos(radians(latitude)) * Math.cos(radians(longitude)),
    Math.cos(radians(latitude)) * Math.sin(radians(longitude)),
    Math.sin(radians(latitude)),
  ];
  const va = toVector(a);
  const vb = toVector(b);
  const points: PathPoint[] = [];
  for (let index = 0; index < count; index += 1) {
    const fraction = count === 1 ? 0 : index / (count - 1);
    // Spherical interpolation; a straight blend when the ends (nearly) coincide.
    const [wa, wb] =
      angle < 1e-12
        ? [1 - fraction, fraction]
        : [
            Math.sin((1 - fraction) * angle) / Math.sin(angle),
            Math.sin(fraction * angle) / Math.sin(angle),
          ];
    const [x, y, z] = [0, 1, 2].map((i) => wa * va[i] + wb * vb[i]);
    points.push({
      latitude: degrees(Math.atan2(z, Math.hypot(x, y))),
      longitude: degrees(Math.atan2(y, x)),
      distance: fraction * total,
    });
  }
  return points;
}

/** Web Mercator position in pixels of the whole world at this zoom. */
export function worldPixel(point: LatLon, zoom: number): [number, number] {
  const size = TILE_SIZE * 2 ** zoom;
  const latitude = Math.max(-85.0511, Math.min(85.0511, point.latitude));
  const sin = Math.sin(radians(latitude));
  return [
    ((point.longitude + 180) / 360) * size,
    (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size,
  ];
}

export function tileId({ z, x, y }: TileKey): string {
  return `${z}/${x}/${y}`;
}

// The four pixels around a point, as whole-world pixel columns and rows, with
// the point's weights between them. Pixel centres sit at half pixels.
function pixelNeighbours(point: LatLon, zoom: number) {
  const [px, py] = worldPixel(point, zoom);
  const gx = px - 0.5;
  const gy = py - 0.5;
  const x0 = Math.floor(gx);
  const y0 = Math.floor(gy);
  return { x0, y0, fx: gx - x0, fy: gy - y0 };
}

// A pixel's tile and place in it, wrapping round the world east–west and
// staying on the map north–south.
function locatePixel(column: number, row: number, zoom: number) {
  const size = TILE_SIZE * 2 ** zoom;
  const x = ((column % size) + size) % size;
  const y = Math.max(0, Math.min(size - 1, row));
  return {
    tile: {
      z: zoom,
      x: Math.floor(x / TILE_SIZE),
      y: Math.floor(y / TILE_SIZE),
    },
    offset: (y % TILE_SIZE) * TILE_SIZE + (x % TILE_SIZE),
  };
}

/** The tiles holding every pixel the points' heights are interpolated from. */
export function tilesFor(points: LatLon[], zoom: number): TileKey[] {
  const tiles = new Map<string, TileKey>();
  for (const point of points) {
    const { x0, y0 } = pixelNeighbours(point, zoom);
    for (const [dx, dy] of [
      [0, 0],
      [1, 0],
      [0, 1],
      [1, 1],
    ]) {
      const { tile } = locatePixel(x0 + dx, y0 + dy, zoom);
      tiles.set(tileId(tile), tile);
    }
  }
  return [...tiles.values()];
}

/**
 * The zoom and path samples to use: the highest zoom from 8 to 12 at which the
 * path is at most 1,000 pixels long, sampled about once per pixel (at least
 * 100 samples). A higher zoom would fetch more tiles only to skip most of
 * their pixels, and fetching tiles is nearly all the page's waiting. Zoom 12
 * pixels are about 38 m at the equator, close to the source data's roughly
 * 30 m.
 */
export function planPath(
  a: LatLon,
  b: LatLon,
): { zoom: number; points: PathPoint[]; tiles: TileKey[] } {
  let zoom = MAX_ZOOM;
  let pixels = pathPixels(a, b, zoom);
  while (zoom > MIN_ZOOM && pixels > MAX_SAMPLES) {
    zoom -= 1;
    pixels = pathPixels(a, b, zoom);
  }
  const count = Math.round(
    Math.max(MIN_SAMPLES, Math.min(MAX_SAMPLES, pixels)),
  );
  const points = samplePath(a, b, count);
  return { zoom, points, tiles: tilesFor(points, zoom) };
}

function pathPixels(a: LatLon, b: LatLon, zoom: number) {
  const [ax, ay] = worldPixel(a, zoom);
  const [bx, by] = worldPixel(b, zoom);
  return Math.hypot(bx - ax, by - ay);
}

/** Terrarium tile colour to metres above sea level. */
export function decodeTerrarium(red: number, green: number, blue: number) {
  return red * 256 + green + blue / 256 - 32768;
}

/** Heights of a decoded RGBA tile image. */
export function decodeTerrariumImage(rgba: Uint8ClampedArray): Float32Array {
  const heights = new Float32Array(rgba.length / 4);
  for (let index = 0; index < heights.length; index += 1) {
    heights[index] = decodeTerrarium(
      rgba[index * 4],
      rgba[index * 4 + 1],
      rgba[index * 4 + 2],
    );
  }
  return heights;
}

/** Ground height at a point, bilinear between the four nearest pixels; null without its tiles. */
export function groundHeight(
  point: LatLon,
  zoom: number,
  tiles: ReadonlyMap<string, HeightTile>,
): number | null {
  const { x0, y0, fx, fy } = pixelNeighbours(point, zoom);
  const at = (dx: number, dy: number) => {
    const { tile, offset } = locatePixel(x0 + dx, y0 + dy, zoom);
    const heights = tiles.get(tileId(tile));
    return heights == null ? null : heights[offset];
  };
  const h00 = at(0, 0);
  const h10 = at(1, 0);
  const h01 = at(0, 1);
  const h11 = at(1, 1);
  if (h00 === null || h10 === null || h01 === null || h11 === null) return null;
  const top = h00 + (h10 - h00) * fx;
  const bottom = h01 + (h11 - h01) * fx;
  return top + (bottom - top) * fy;
}

/** How far the Earth's curve raises terrain between two ends d1 and d2 metres away. */
export function curvatureRise(d1: number, d2: number): number {
  return (d1 * d2) / (2 * EARTH_RADIUS_FACTOR * EARTH_RADIUS_M);
}

/** Radius of the first Fresnel zone d1 and d2 metres from the ends. */
export function fresnelRadius(d1: number, d2: number): number {
  const total = d1 + d2;
  return total === 0 ? 0 : Math.sqrt((WAVELENGTH_M * d1 * d2) / total);
}

/** Ground near a node that matches its high reported altitude. */
export interface Site extends LatLon {
  /** Metres from the node's position. */
  offset: number;
  /** The site's ground height at SITE_ZOOM. */
  ground: number;
}

/** The search for a site around a node's position. */
export interface SiteSearch {
  /** The node's position, where the site was looked for. */
  from: LatLon;
  /** Ground height at the node's position. */
  fromGround: number;
  /** How far the position may be from the node, by its rounding; 0 when exact. */
  roundingKm: number;
  radiusKm: number;
  site: Site | null;
}

export interface EndHeight {
  /** Ground height at the end; null when its terrain is unavailable. */
  ground: number | null;
  /** Where the sight line starts; null when neither height is known. */
  height: number | null;
  source: "reported" | "site" | "ground";
  /** A reported altitude that was not used. */
  ignored: number | null;
  /** The site search when the reported altitude was too high for the ground. */
  search: SiteSearch | null;
}

/**
 * The sight line starts at the reported altitude when that is within 300 m
 * above the ground (GPS error, terrain model error, rooftops, masts), or at a
 * site the search found, where the end then stands. Otherwise it starts an
 * assumed antenna height above the ground.
 */
export function endHeight(
  ground: number | null,
  reported: number | null,
  search: SiteSearch | null = null,
): EndHeight {
  if (ground === null) {
    return {
      ground,
      height: reported,
      source: "reported",
      ignored: null,
      search,
    };
  }
  if (reported !== null && search?.site) {
    return {
      ground,
      height: Math.max(reported, ground + ASSUMED_ANTENNA_HEIGHT_M),
      source: "site",
      ignored: null,
      search,
    };
  }
  if (
    reported !== null &&
    reported >= ground &&
    reported <= ground + MAX_ALTITUDE_ABOVE_GROUND_M
  ) {
    return {
      ground,
      height: reported,
      source: "reported",
      ignored: null,
      search,
    };
  }
  return {
    ground,
    height: ground + ASSUMED_ANTENNA_HEIGHT_M,
    source: "ground",
    ignored: reported,
    search,
  };
}

/** Whether the end's reported altitude was ignored as too high, so a site is worth looking for. */
export function needsSite(end: EndHeight): boolean {
  return (
    end.source === "ground" &&
    end.search === null &&
    end.ignored !== null &&
    end.ground !== null &&
    end.ignored > end.ground
  );
}

/**
 * How far, in km, a position may sit from the node by its deliberate
 * rounding: half the rounding cell's diagonal; 0 when it looks exact. The
 * firmware keeps the top p bits of each 1e-7° coordinate and centres it in
 * the cell, which leaves 31 − p trailing zero bits.
 */
export function positionRoundingKm({ latitude, longitude }: LatLon): number {
  const bits = Math.min(zeroBits(latitude), zeroBits(longitude));
  // Ten or more on both by chance is about one position in a million.
  if (bits < 10) return 0;
  const half = 2 ** bits * 1e-7 * ((Math.PI * EARTH_RADIUS_M) / 180_000);
  return half * Math.hypot(1, Math.cos(radians(latitude)));
}

function zeroBits(coordinate: number): number {
  let value = Math.abs(Math.round(coordinate * 1e7));
  if (value === 0) return 31;
  let bits = 0;
  while (value % 2 === 0) {
    value /= 2;
    bits += 1;
  }
  return bits;
}

/** The radius searched for a site around a position. */
export function siteRadiusKm(point: LatLon): number {
  return Math.min(
    SITE_MAX_KM,
    Math.max(SITE_MIN_KM, positionRoundingKm(point) + 0.5),
  );
}

// The whole-world pixel columns and rows of a square around a point.
function pixelBox(point: LatLon, radiusKm: number, zoom: number) {
  const dLat = degrees((radiusKm * 1000) / EARTH_RADIUS_M);
  const dLon = dLat / Math.cos(radians(point.latitude));
  const [x0, y0] = worldPixel(
    { latitude: point.latitude + dLat, longitude: point.longitude - dLon },
    zoom,
  );
  const [x1, y1] = worldPixel(
    { latitude: point.latitude - dLat, longitude: point.longitude + dLon },
    zoom,
  );
  return {
    x0: Math.floor(x0),
    y0: Math.floor(y0),
    x1: Math.floor(x1),
    y1: Math.floor(y1),
  };
}

/** The tiles a site search around a point reads. */
export function siteTiles(point: LatLon, radiusKm: number): TileKey[] {
  const { x0, y0, x1, y1 } = pixelBox(point, radiusKm, SITE_ZOOM);
  const tiles: TileKey[] = [];
  for (
    let x = Math.floor(x0 / TILE_SIZE);
    x <= Math.floor(x1 / TILE_SIZE);
    x += 1
  ) {
    for (
      let y = Math.floor(y0 / TILE_SIZE);
      y <= Math.floor(y1 / TILE_SIZE);
      y += 1
    ) {
      tiles.push(locatePixel(x * TILE_SIZE, y * TILE_SIZE, SITE_ZOOM).tile);
    }
  }
  return tiles;
}

/**
 * The SITE_ZOOM pixel nearest the point within radiusKm whose ground reaches
 * minHeight; null when none does, undefined when a tile is missing.
 */
export function findSite(
  point: LatLon,
  minHeight: number,
  radiusKm: number,
  tiles: ReadonlyMap<string, HeightTile>,
): Site | null | undefined {
  const { x0, y0, x1, y1 } = pixelBox(point, radiusKm, SITE_ZOOM);
  const size = TILE_SIZE * 2 ** SITE_ZOOM;
  let best: Site | null = null;
  for (let row = y0; row <= y1; row += 1) {
    const latitude = degrees(
      Math.atan(Math.sinh(Math.PI * (1 - (2 * (row + 0.5)) / size))),
    );
    for (let column = x0; column <= x1; column += 1) {
      const { tile, offset } = locatePixel(column, row, SITE_ZOOM);
      const heights = tiles.get(tileId(tile));
      if (heights == null) return undefined;
      const ground = heights[offset];
      if (ground < minHeight) continue;
      const site = {
        latitude,
        longitude: ((column + 0.5) / size) * 360 - 180,
      };
      const distance = distanceMeters(point, site);
      if (
        distance <= radiusKm * 1000 &&
        (best === null || distance < best.offset)
      ) {
        best = { ...site, offset: distance, ground };
      }
    }
  }
  return best;
}

const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

/** The eight-point compass direction from one point to another. */
export function compassPoint(from: LatLon, to: LatLon): string {
  const phi1 = radians(from.latitude);
  const phi2 = radians(to.latitude);
  const dLon = radians(to.longitude - from.longitude);
  const bearing = degrees(
    Math.atan2(
      Math.sin(dLon) * Math.cos(phi2),
      Math.cos(phi1) * Math.sin(phi2) -
        Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLon),
    ),
  );
  return COMPASS[Math.round((((bearing % 360) + 360) % 360) / 45) % 8];
}

export function clearanceTier(ratio: number): ClearanceTier {
  if (ratio >= CLEAR_RATIO) return "clear";
  if (ratio >= 0) return "partial";
  return "blocked";
}

export interface ProfileSample extends PathPoint {
  ground: number | null;
  /** How far the Earth's curve raises the ground here. */
  curve: number;
  /** Ground raised by the Earth's curve. */
  terrain: number | null;
  /** The straight sight line, measured against the raised terrain. */
  sightLine: number | null;
  /** The sight line's true height above sea level: it sags by the curve. */
  sightAltitude: number | null;
  fresnel: number;
  /** Metres from the raised terrain up to the sight line. */
  clearance: number | null;
  /** Clearance as a share of the Fresnel radius; Infinity at the ends. */
  ratio: number | null;
  tier: ClearanceTier | null;
}

export interface Profile {
  distance: number;
  samples: ProfileSample[];
  a: EndHeight;
  b: EndHeight;
  /** The sample with the least clearance ratio; null without terrain. */
  worst: ProfileSample | null;
  /** True when some samples have no terrain. */
  incomplete: boolean;
}

export function buildProfile(
  points: PathPoint[],
  grounds: (number | null)[],
  reportedA: number | null,
  reportedB: number | null,
  searches: [SiteSearch | null, SiteSearch | null] = [null, null],
): Profile {
  const distance = points[points.length - 1]?.distance ?? 0;
  const a = endHeight(grounds[0] ?? null, reportedA, searches[0]);
  const b = endHeight(
    grounds[grounds.length - 1] ?? null,
    reportedB,
    searches[1],
  );
  let worst: ProfileSample | null = null;
  const samples = points.map((point, index): ProfileSample => {
    const d1 = point.distance;
    const d2 = distance - d1;
    const ground = grounds[index];
    const curve = curvatureRise(d1, d2);
    const terrain = ground === null ? null : ground + curve;
    // The ends use their heights exactly, so rounding cannot sink one below the ground.
    const sightLine =
      a.height === null || b.height === null
        ? null
        : index === 0 || distance === 0
          ? a.height
          : index === points.length - 1
            ? b.height
            : a.height + ((b.height - a.height) * d1) / distance;
    const fresnel = fresnelRadius(d1, d2);
    const clearance =
      terrain === null || sightLine === null ? null : sightLine - terrain;
    // At the ends the zone has no width; they start at or above the ground.
    const ratio =
      clearance === null
        ? null
        : fresnel === 0
          ? clearance >= 0
            ? Infinity
            : -Infinity
          : clearance / fresnel;
    const sample = {
      ...point,
      ground,
      curve,
      terrain,
      sightLine,
      sightAltitude: sightLine === null ? null : sightLine - curve,
      fresnel,
      clearance,
      ratio,
      tier: ratio === null ? null : clearanceTier(ratio),
    };
    if (ratio !== null && (worst === null || ratio < worst.ratio!)) {
      worst = sample;
    }
    return sample;
  });
  return {
    distance,
    samples,
    a,
    b,
    worst,
    incomplete: grounds.some((ground) => ground === null),
  };
}

export interface TierRun {
  tier: ClearanceTier;
  /** Consecutive samples; each run shares its last sample with the next, so lines join. */
  samples: ProfileSample[];
}

/** Consecutive samples of one tier; samples without terrain break the runs. */
export function tierRuns(samples: ProfileSample[]): TierRun[] {
  const runs: TierRun[] = [];
  let current: TierRun | null = null;
  for (const sample of samples) {
    if (sample.tier === null) {
      current = null;
      continue;
    }
    if (current !== null && current.tier === sample.tier) {
      current.samples.push(sample);
      continue;
    }
    current?.samples.push(sample);
    current = { tier: sample.tier, samples: [sample] };
    runs.push(current);
  }
  return runs;
}
