import type { StyleSpecification } from "maplibre-gl";

// Background maps the viewer can switch between. Each is a free public
// service meant for light use; the first one is the default.

// Node names are drawn with this font, so raster styles need somewhere to load it from.
const OPENFREEMAP_GLYPHS =
  "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf";

function rasterStyle(
  tiles: string[],
  attribution: string,
  maxzoom: number,
): StyleSpecification {
  return {
    version: 8,
    glyphs: OPENFREEMAP_GLYPHS,
    sources: {
      basemap: { type: "raster", tiles, tileSize: 256, maxzoom, attribution },
    },
    layers: [{ id: "basemap", type: "raster", source: "basemap" }],
  };
}

export interface Basemap {
  id: string;
  label: string;
  style: string | StyleSpecification;
  /** False on dark maps, where node names read better light on dark. */
  darkLabels?: boolean;
  /** True where white clusters blend into pale land and roads. */
  orangeClusters?: boolean;
}

export const BASEMAPS: Basemap[] = [
  {
    id: "liberty",
    label: "Streets",
    style: "https://tiles.openfreemap.org/styles/liberty",
    orangeClusters: true,
  },
  {
    // Contour lines and hill shading help judge line of sight between radios.
    id: "topo",
    label: "Topographic",
    style: rasterStyle(
      ["a", "b", "c"].map(
        (s) => `https://${s}.tile.opentopomap.org/{z}/{x}/{y}.png`,
      ),
      'Map data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM · Style © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
      17,
    ),
  },
  {
    id: "light",
    label: "Light",
    style: "https://tiles.openfreemap.org/styles/positron",
    orangeClusters: true,
  },
  {
    id: "dark",
    label: "Dark",
    style: "https://tiles.openfreemap.org/styles/dark",
    darkLabels: false,
  },
];

export function findBasemap(id: string): Basemap {
  return BASEMAPS.find((basemap) => basemap.id === id) ?? BASEMAPS[0];
}
