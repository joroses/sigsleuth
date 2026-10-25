import type { Geometry } from "geojson";
import type { Map, MapGeoJSONFeature, PointLike } from "maplibre-gl";

// How far from a line, in pixels, a click or hover still takes it: wider on
// touch screens, where a finger covers more than a mouse pointer.
const MOUSE_REACH_PX = 6;
const TOUCH_REACH_PX = 12;

function reachPx(): number {
  return typeof window.matchMedia === "function" &&
    window.matchMedia("(pointer: coarse)").matches
    ? TOUCH_REACH_PX
    : MOUSE_REACH_PX;
}

/**
 * The line in the given layers nearest the point, within reach of it, so thin
 * lines need no exact aim. Lines equally near go to the top-most.
 */
export function nearestLine(
  map: Map,
  point: { x: number; y: number },
  layers: string[],
): MapGeoJSONFeature | undefined {
  const present = layers.filter((layer) => map.getLayer(layer) !== undefined);
  if (present.length === 0) return undefined;
  const reach = reachPx();
  const box: [PointLike, PointLike] = [
    [point.x - reach, point.y - reach],
    [point.x + reach, point.y + reach],
  ];
  let nearest: MapGeoJSONFeature | undefined;
  let nearestDistance = Infinity;
  for (const feature of map.queryRenderedFeatures(box, { layers: present })) {
    const distance = Math.min(
      ...lineParts(feature.geometry).map((part) =>
        distanceToPath(
          point,
          part.map(([longitude, latitude]) =>
            map.project([longitude, latitude]),
          ),
        ),
      ),
    );
    if (distance < nearestDistance) {
      nearest = feature;
      nearestDistance = distance;
    }
  }
  return nearest;
}

function lineParts(geometry: Geometry): number[][][] {
  if (geometry.type === "LineString") return [geometry.coordinates];
  if (geometry.type === "MultiLineString") return geometry.coordinates;
  return [];
}

/** Pixels from a point to the nearest place on a path of screen points. */
export function distanceToPath(
  point: { x: number; y: number },
  path: { x: number; y: number }[],
): number {
  if (path.length === 1)
    return Math.hypot(point.x - path[0].x, point.y - path[0].y);
  let nearest = Infinity;
  for (let index = 1; index < path.length; index += 1) {
    const start = path[index - 1];
    const end = path[index];
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const lengthSquared = dx * dx + dy * dy;
    const along =
      lengthSquared === 0
        ? 0
        : Math.max(
            0,
            Math.min(
              1,
              ((point.x - start.x) * dx + (point.y - start.y) * dy) /
                lengthSquared,
            ),
          );
    nearest = Math.min(
      nearest,
      Math.hypot(
        point.x - start.x - along * dx,
        point.y - start.y - along * dy,
      ),
    );
  }
  return nearest;
}
