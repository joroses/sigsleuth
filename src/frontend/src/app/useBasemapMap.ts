import { type DependencyList, useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import { BASEMAPS, type Basemap, findBasemap } from "./basemaps";

/**
 * Why there is no map: the browser cannot draw one (no WebGL), or the
 * basemap failed before its style loaded.
 */
export type MapFailure = "unavailable" | "basemap";

export const MAP_FAILURE_MESSAGES: Record<MapFailure, string> = {
  unavailable: "The interactive map is unavailable in this browser.",
  basemap: "The map could not load. Refresh the page to try again.",
};

interface BasemapMapOptions {
  /** False leaves the container empty, with no map. */
  enabled?: boolean;
  /** Construction options such as the first view, asked for when the map is created. */
  view?: () => Partial<maplibregl.MapOptions>;
  /**
   * The basemap to start on. The default (Streets) unless given: only the
   * Map page offers and remembers a choice.
   */
  basemap?: string;
  /** North up and no tilt, with zoom buttons at the top right. */
  flat?: boolean;
  /** Credit for data drawn on top of the basemap, added to its own. */
  attribution?: string;
  /** Called once the map exists, for markers and handlers; may return a cleanup. */
  setup?: (map: maplibregl.Map, basemap: Basemap) => (() => void) | void;
  /** Called when the first style is parsed, without waiting for tiles: add layers here. */
  onStyleLoad?: (map: maplibregl.Map, basemap: Basemap) => void;
}

/**
 * Creates a MapLibre map on the given basemap in `container`, and removes
 * it on unmount or when `deps` change. `map` is null until the first style
 * has loaded and `onStyleLoad` has run, so effects that fill sources can
 * wait for it. Errors after that (a missing tile) leave the map usable and
 * are not reported.
 */
export function useBasemapMap(
  options: BasemapMapOptions,
  deps: DependencyList,
) {
  const container = useRef<HTMLDivElement | null>(null);
  const [map, setMap] = useState<maplibregl.Map | null>(null);
  const [failure, setFailure] = useState<MapFailure | null>(null);

  useEffect(() => {
    if (container.current === null || options.enabled === false) return;
    const { view, flat = false, setup, onStyleLoad } = options;
    const basemap = findBasemap(options.basemap ?? BASEMAPS[0].id);
    let created: maplibregl.Map;
    const constructionStart = performance.now();
    try {
      created = new maplibregl.Map({
        attributionControl: {
          compact: true,
          customAttribution: options.attribution,
        },
        ...(flat
          ? { dragRotate: false, pitchWithRotate: false, touchPitch: false }
          : {}),
        ...view?.(),
        container: container.current,
        style: basemap.style,
      });
      performance.measure("map: construction", { start: constructionStart });
    } catch {
      queueMicrotask(() => setFailure("unavailable"));
      return;
    }
    if (flat) {
      created.touchZoomRotate.disableRotation();
      created.keyboard.disableRotation();
      created.addControl(
        new maplibregl.NavigationControl({ showCompass: false }),
        "top-right",
      );
    }
    let styleLoaded = false;
    // Node layers need the style before they can draw. A failed first style
    // asks the reader to reload; add a fallback or retry only if these
    // failures become a normal-use problem. Later tile failures leave layers.
    created.on("error", () => {
      if (!styleLoaded) setFailure("basemap");
    });
    const cleanup = setup?.(created, basemap);
    created.once("style.load", () => {
      styleLoaded = true;
      onStyleLoad?.(created, basemap);
      setFailure(null);
      setMap(created);
    });
    return () => {
      cleanup?.();
      setMap(null);
      created.remove();
    };
    // The options are read when the map is created; only deps recreate it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { container, map, failure };
}
