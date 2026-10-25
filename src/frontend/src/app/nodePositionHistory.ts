import { LngLat } from "maplibre-gl";

import type { NodePositionHistory } from "./api";

// Display spacing, not evidence of movement; reported precision varies.
const MIN_SPACING_METERS = 3_000;

export function spacedPositions(
  items: NodePositionHistory["items"],
): NodePositionHistory["items"] {
  const shown: NodePositionHistory["items"] = [];
  let last: LngLat | null = null;
  for (const item of items) {
    const point = new LngLat(item.longitude, item.latitude);
    if (last !== null && point.distanceTo(last) < MIN_SPACING_METERS) continue;
    shown.push(item);
    last = point;
  }
  return shown;
}
