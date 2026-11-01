import * as maplibregl from "maplibre-gl";

import type { CoverageExtent, MapNode } from "./api";
import { type CellAddress, cellBounds } from "./coverageLayer";
import { LINK_MIN_ZOOM } from "./mapLinkLayers";
import { isNarrowScreen } from "./narrowScreen";

// Half the side of the square kept in view around a chosen node: about 300 × 300 km.
const FOCUS_HALF_SIDE_KM = 150;
const KM_PER_DEGREE_LATITUDE = 111.32;

// On a phone the sidebar sheet covers up to 55% of the map's bottom (see
// styles.css), so a chosen place is centred in the part above it.
function sheetOffset(instance: maplibregl.Map): [number, number] {
  if (!isNarrowScreen()) return [0, 0];
  return [0, -instance.getContainer().clientHeight * 0.275];
}

// Room around places fitted in view: a margin, and on a phone the sheet's
// height, split above and below since sheetOffset then lifts them above it.
function fitPadding(instance: maplibregl.Map): maplibregl.PaddingOptions {
  const half = -sheetOffset(instance)[1];
  return { top: 60 + half, bottom: 60 + half, left: 60, right: 60 };
}

// How far the open filter panel reaches over the map's left side.
export function filterPanelWidth(instance: maplibregl.Map): number {
  const container = instance.getContainer();
  const panel = container.parentElement?.querySelector(
    ".map-filter-panel:not(.map-filter-panel-closed)",
  );
  if (panel == null) return 0;
  return (
    panel.getBoundingClientRect().right - container.getBoundingClientRect().left
  );
}

function moveCamera(
  instance: maplibregl.Map,
  options: maplibregl.FlyToOptions,
) {
  // flyTo drops the sheet offset when reduced motion is enabled.
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches)
    instance.easeTo({ ...options, duration: 0 });
  else instance.flyTo(options);
}

// Below this zoom, nodes inside a cluster stay hidden during a selection.
const UNCLUSTERED_ZOOM = 5;

// Whether a node shows as the map is: out of the clusters, on screen, and
// clear of the filter panel and a phone's sidebar sheet.
export function inView(instance: maplibregl.Map, node: MapNode): boolean {
  if (instance.getZoom() < UNCLUSTERED_ZOOM) return false;
  const { x, y } = instance.project([node.longitude, node.latitude]);
  const container = instance.getContainer();
  const sheetHeight = -2 * sheetOffset(instance)[1];
  return (
    x >= filterPanelWidth(instance) &&
    x <= container.clientWidth &&
    y >= 0 &&
    y <= container.clientHeight - sheetHeight
  );
}

// Centre an unconnected node with about 300 km around it, unless it is in view
// and the camera is not moving.
function focusNode(instance: maplibregl.Map, node: MapNode) {
  if (!instance.isMoving() && inView(instance, node)) return;
  const { latitude, longitude } = node;
  const latitudeDelta = FOCUS_HALF_SIDE_KM / KM_PER_DEGREE_LATITUDE;
  // Clamped so the box stays finite near the poles.
  const longitudeDelta =
    latitudeDelta / Math.max(Math.cos((latitude * Math.PI) / 180), 0.05);
  const camera = instance.cameraForBounds([
    [longitude - longitudeDelta, latitude - latitudeDelta],
    [longitude + longitudeDelta, latitude + latitudeDelta],
  ]);
  moveCamera(instance, {
    center: [longitude, latitude],
    zoom: Math.max(camera?.zoom ?? 0, instance.getZoom()),
    offset: sheetOffset(instance),
  });
}

// Zoom out, if either node is off screen, until both are in view, but not below
// the zoom where links are drawn; a very long link may still be cut off.
export function focusLink(instance: maplibregl.Map, a: MapNode, b: MapNode) {
  const ends: [number, number][] = [a, b].map((node) => [
    node.longitude,
    node.latitude,
  ]);
  const bounds = instance.getBounds();
  if (ends.every((end) => bounds.contains(end))) return;
  const camera = instance.cameraForBounds(
    new maplibregl.LngLatBounds(ends[0], ends[0]).extend(ends[1]),
    { padding: fitPadding(instance) },
  );
  if (camera === undefined) return;
  moveCamera(instance, {
    center: camera.center,
    zoom: Math.min(
      Math.max(camera.zoom ?? 0, LINK_MIN_ZOOM),
      instance.getZoom(),
    ),
    offset: sheetOffset(instance),
  });
}

// Fit a sender and the gateways that reported its texts, but not below the zoom
// where clusters split; without zoomIn, only zooming out. A sender without a
// position (null) is fitted to its gateways alone.
export function focusTextReach(
  instance: maplibregl.Map,
  sender: MapNode | null,
  gateways: MapNode[],
  zoomIn = true,
) {
  if (gateways.length === 0) {
    if (sender !== null) focusNode(instance, sender);
    return;
  }
  const bounds = new maplibregl.LngLatBounds();
  for (const node of sender === null ? gateways : [sender, ...gateways])
    bounds.extend([node.longitude, node.latitude]);
  const camera = instance.cameraForBounds(bounds, {
    padding: fitPadding(instance),
  });
  if (camera === undefined) return;
  const zoom = camera.zoom ?? 0;
  moveCamera(instance, {
    center: camera.center,
    zoom: Math.max(
      zoomIn ? zoom : Math.min(zoom, instance.getZoom()),
      UNCLUSTERED_ZOOM,
    ),
    offset: sheetOffset(instance),
  });
}

// Show a node, its coverage and other nodes: only when some are out of
// view, and without zooming in, unless zoomIn fits them whatever the view.
export function focusCoverage(
  instance: maplibregl.Map,
  node: MapNode | null,
  extent: CoverageExtent | null,
  zoomIn: boolean,
  otherNodes: MapNode[] = [],
  cell?: CellAddress,
) {
  if (extent === null && cell === undefined) {
    focusTextReach(instance, node, otherNodes);
    return;
  }
  const bounds = new maplibregl.LngLatBounds();
  if (extent !== null) {
    bounds.extend([extent.west, extent.south]);
    bounds.extend([extent.east, extent.north]);
  }
  if (cell !== undefined) {
    const [west, south, east, north] = cellBounds(cell);
    bounds.extend([west, south]);
    bounds.extend([east, north]);
  }
  if (node !== null) bounds.extend([node.longitude, node.latitude]);
  for (const other of otherNodes)
    bounds.extend([other.longitude, other.latitude]);
  const view = instance.getBounds();
  if (
    !zoomIn &&
    view.contains(bounds.getSouthWest()) &&
    view.contains(bounds.getNorthEast())
  )
    return;
  const camera = instance.cameraForBounds(bounds, {
    padding: fitPadding(instance),
  });
  if (camera === undefined) return;
  moveCamera(instance, {
    center: camera.center,
    zoom: Math.min(
      Math.max(camera.zoom ?? 0, otherNodes.length > 0 ? LINK_MIN_ZOOM : 0),
      zoomIn ? 14 : instance.getZoom(),
    ),
    offset: sheetOffset(instance),
  });
}
