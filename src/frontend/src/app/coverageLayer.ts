import type {
  Feature,
  FeatureCollection,
  LineString,
  Polygon,
  Position,
} from "geojson";
import type { ExpressionSpecification, Map } from "maplibre-gl";

import type {
  CoverageCell,
  CoverageCellList,
  CoverageHeardNode,
  MapNode,
} from "./api";
import { relayByteText, relayGuessText } from "./nodeDisplay";

// The map's signal survey coverage: square cells coloured by their SNR
// margin, and lines from a chosen cell to the nodes heard there. Cells are Web
// Mercator tiles, so they are exactly square on the map; the server picks
// their level from the zoom.

export const COVERAGE_SOURCE = "coverage-cells";
export const COVERAGE_FILL_LAYER = "coverage-cells-fill";
const COVERAGE_OUTLINE_LAYER = "coverage-cells-outline";
export const COVERAGE_LINES_SOURCE = "coverage-lines";
const COVERAGE_CASING_LAYER = "coverage-lines-casing";
const COVERAGE_CERTAIN_LAYER = "coverage-lines-certain";
const COVERAGE_GUESSED_LAYER = "coverage-lines-guessed";
export const COVERAGE_SOURCES = [COVERAGE_SOURCE, COVERAGE_LINES_SOURCE];

/** A cell as the API names it. */
export interface CellAddress {
  level: number;
  x: number;
  y: number;
}

/** The map area and zoom the cells are fetched for. */
export interface CoverageView {
  west: number;
  south: number;
  east: number;
  north: number;
  zoom: number;
}

/**
 * The map's view when it last stopped, and the area cells are fetched for:
 * the view with half its size added on every side (padded), so a short pan
 * shows cells at once and asks for nothing.
 */
export interface CoverageArea {
  view: CoverageView;
  fetched: CoverageView;
}

/**
 * The area once the map stops: the same fetched area while the view stays
 * inside it at the same whole zoom, which sets the cells' level, else a new
 * padded one.
 */
export function nextCoverageArea(
  area: CoverageArea | null,
  view: CoverageView,
): CoverageArea {
  const fetched = area?.fetched;
  if (
    fetched !== undefined &&
    Math.floor(fetched.zoom) === Math.floor(view.zoom) &&
    view.west >= fetched.west &&
    view.east <= fetched.east &&
    view.south >= fetched.south &&
    view.north <= fetched.north
  )
    return { ...area!, view };
  const padX = (view.east - view.west) / 2;
  const padY = (view.north - view.south) / 2;
  return {
    view,
    fetched: {
      west: view.west - padX,
      south: view.south - padY,
      east: view.east + padX,
      north: view.north + padY,
      zoom: view.zoom,
    },
  };
}

/** The cells of a list that overlap the view, for counts of what is in view. */
export function cellsInView(
  list: CoverageCellList,
  view: CoverageView,
): CoverageCellList {
  return {
    ...list,
    cells: list.cells.filter((cell) => {
      const [west, south, east, north] = cellBounds({
        ...cell,
        level: list.level,
      });
      return (
        east > view.west &&
        west < view.east &&
        north > view.south &&
        south < view.north
      );
    }),
  };
}

/**
 * What the coverage layer shows with a node selected: "heard" the
 * measurements where it was the node the survey node heard (its radio
 * footprint, with its own packets heard directly); "all" ignores it.
 */
export type CoverageNodeMode = "heard" | "all";

export function coverageCellsUrl(
  view: CoverageView,
  node: number | null,
  channel: string | null,
): string {
  const round = (value: number) => value.toFixed(4);
  const query = new URLSearchParams({
    // MapLibre's bounds can run past the world's edges when zoomed out.
    west: round(Math.max(view.west, -180)),
    south: round(Math.max(view.south, -85)),
    east: round(Math.min(view.east, 180)),
    north: round(Math.min(view.north, 85)),
    zoom: view.zoom.toFixed(1),
  });
  if (node !== null) query.set("node", String(node));
  if (channel !== null) query.set("channel", channel);
  return `/api/v1/coverage/cells?${query}`;
}

/** The area holding every cell where survey nodes heard a node. */
export function coverageExtentUrl(node: number): string {
  return `/api/v1/coverage/extent?node=${node}`;
}

/** One cell's detail, with the same filters as the cells drawn. */
export function coverageCellUrl(
  cell: CellAddress,
  node: number | null,
  channel: string | null,
): string {
  const query = new URLSearchParams();
  if (node !== null) query.set("node", String(node));
  if (channel !== null) query.set("channel", channel);
  const url = `/api/v1/coverage/cells/${cell.level}/${cell.x}/${cell.y}`;
  return `${query}` === "" ? url : `${url}?${query}`;
}

function tileLongitude(x: number, level: number): number {
  return (x / 2 ** level) * 360 - 180;
}

function tileLatitude(y: number, level: number): number {
  const n = Math.PI * (1 - (2 * y) / 2 ** level);
  return (Math.atan(Math.sinh(n)) * 180) / Math.PI;
}

/** A cell's corners as [west, south, east, north]. */
export function cellBounds({ level, x, y }: CellAddress) {
  return [
    tileLongitude(x, level),
    tileLatitude(y + 1, level),
    tileLongitude(x + 1, level),
    tileLatitude(y, level),
  ] as const;
}

/**
 * Margin colours over 0-25 dB above the floor: red at the edge of decoding,
 * amber, then greens. The links' tiers stop at 10 dB, where most survey
 * measurements already are, so cells need a longer scale. Presentation only.
 */
export const MARGIN_STOPS: [number, string][] = [
  [0, "#d0453a"],
  [6, "#e0a526"],
  [13, "#8fc04a"],
  [25, "#1f7a3f"],
];
const NO_MARGIN_COLOR = "#9aa39e";

/** A margin's colour on the cells' scale, for the sidebar; null has none. */
export function marginColor(margin: number | null): string {
  if (margin === null) return NO_MARGIN_COLOR;
  const upper = MARGIN_STOPS.findIndex(([db]) => db >= margin);
  if (upper <= 0)
    return MARGIN_STOPS[upper === 0 ? 0 : MARGIN_STOPS.length - 1][1];
  const [lowDb, lowColor] = MARGIN_STOPS[upper - 1];
  const [highDb, highColor] = MARGIN_STOPS[upper];
  const share = (margin - lowDb) / (highDb - lowDb);
  const channel = (color: string, at: number) =>
    parseInt(color.slice(at, at + 2), 16);
  const mixed = [1, 3, 5].map((at) =>
    Math.round(
      channel(lowColor, at) +
        share * (channel(highColor, at) - channel(lowColor, at)),
    )
      .toString(16)
      .padStart(2, "0"),
  );
  return `#${mixed.join("")}`;
}

const MARGIN_COLOR: ExpressionSpecification = [
  "case",
  ["==", ["get", "average_margin"], null],
  NO_MARGIN_COLOR,
  [
    "interpolate",
    ["linear"],
    ["get", "average_margin"],
    ...MARGIN_STOPS.flat(),
  ],
];

interface CellProperties extends CoverageCell {
  level: number;
}

export type CoverageFeatureCollection = FeatureCollection<
  Polygon,
  CellProperties
>;

export function buildCoverageFeatureCollection(
  list: CoverageCellList | null,
): CoverageFeatureCollection {
  if (list === null) return { type: "FeatureCollection", features: [] };
  return {
    type: "FeatureCollection",
    features: list.cells.map((cell) => {
      const [west, south, east, north] = cellBounds({
        ...cell,
        level: list.level,
      });
      return {
        type: "Feature",
        id: `${list.level}/${cell.x}/${cell.y}`,
        properties: { ...cell, level: list.level },
        geometry: {
          type: "Polygon",
          coordinates: [
            [
              [west, south],
              [east, south],
              [east, north],
              [west, north],
              [west, south],
            ],
          ],
        },
      };
    }),
  };
}

interface LineProperties {
  nodeId: number;
  certain: boolean;
}

export type CoverageLineCollection = FeatureCollection<
  LineString,
  LineProperties
>;

/**
 * Whether a heard node was named from a direct reception: the survey node's
 * own, or one a survey heard nearby that no other node with the relay byte
 * matches (survey_link). Every other name is a guess.
 */
export function isCertain(how: CoverageHeardNode["how"]): boolean {
  return how === "direct" || how === "survey_link";
}

/**
 * Lines from a cell's centre to each heard node the map draws: solid when it
 * is certain (isCertain), dashed when guessed.
 */
export function buildCoverageLines(
  centre: Position | null,
  heard: CoverageHeardNode[],
  nodesById: ReadonlyMap<number, MapNode>,
): CoverageLineCollection {
  const features: Feature<LineString, LineProperties>[] = [];
  if (centre !== null)
    for (const node of heard) {
      const drawn = nodesById.get(node.node_id);
      if (drawn === undefined) continue;
      features.push({
        type: "Feature",
        properties: {
          nodeId: node.node_id,
          certain: isCertain(node.how),
        },
        geometry: {
          type: "LineString",
          coordinates: [centre, [drawn.longitude, drawn.latitude]],
        },
      });
    }
  return { type: "FeatureCollection", features };
}

/** The cell layers, under beforeId, and the line layers above them. */
export function addCoverageLayers(
  map: Map,
  cells: CoverageFeatureCollection,
  lines: CoverageLineCollection,
  beforeId: string,
): void {
  map.addSource(COVERAGE_SOURCE, { type: "geojson", data: cells });
  map.addLayer(
    {
      id: COVERAGE_FILL_LAYER,
      type: "fill",
      source: COVERAGE_SOURCE,
      paint: {
        "fill-color": MARGIN_COLOR,
        "fill-opacity": [
          "case",
          ["boolean", ["feature-state", "hover"], false],
          0.75,
          0.5,
        ],
      },
    },
    beforeId,
  );
  map.addLayer(
    {
      id: COVERAGE_OUTLINE_LAYER,
      type: "line",
      source: COVERAGE_SOURCE,
      minzoom: 11,
      paint: { "line-color": MARGIN_COLOR, "line-width": 0.5 },
    },
    beforeId,
  );
  map.addSource(COVERAGE_LINES_SOURCE, { type: "geojson", data: lines });
  // Dark slate lines on a white edge read on pale and dark basemaps alike,
  // and over every cell colour; a guessed line's gaps show the white edge.
  map.addLayer(
    {
      id: COVERAGE_CASING_LAYER,
      type: "line",
      source: COVERAGE_LINES_SOURCE,
      paint: { "line-color": "#ffffff", "line-width": 4 },
    },
    beforeId,
  );
  for (const [id, certain] of [
    [COVERAGE_CERTAIN_LAYER, true],
    [COVERAGE_GUESSED_LAYER, false],
  ] as const)
    map.addLayer(
      {
        id,
        type: "line",
        source: COVERAGE_LINES_SOURCE,
        filter: ["==", ["get", "certain"], certain],
        paint: {
          "line-color": "#1e293b",
          "line-width": 2,
          ...(certain ? {} : { "line-dasharray": [2, 2] }),
        },
      },
      beforeId,
    );
}

/**
 * A hovered cell's totals. Its properties come back from MapLibre, which
 * drops nulls.
 */
export function cellText(cell: Record<string, unknown>): string {
  const margin = cell.average_margin;
  const nodes = Number(cell.nodes);
  return `${Number(cell.measurements).toLocaleString()} measurements · ${
    typeof margin === "number"
      ? `${margin.toFixed(1)} dB average margin`
      : "no SNR"
  } · ${nodes.toLocaleString()} heard ${nodes === 1 ? "node" : "nodes"}`;
}

/** The plain words for how a heard node was identified. */
export function heardHowText(node: CoverageHeardNode): string {
  const byte = relayByteText(node.node_id & 0xff);
  switch (node.how) {
    case "direct":
      return "Heard directly";
    case "survey_link":
      return `Heard directly nearby: the only node ending in ${byte} a survey heard`;
    case "unknown":
      return `Unknown relay ending in ${byte}`;
    default:
      return relayGuessText(node.how, byte);
  }
}
