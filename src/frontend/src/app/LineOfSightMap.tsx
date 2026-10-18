import { useEffect, useRef } from "react";
import * as maplibregl from "maplibre-gl";

import {
  TIER_COLORS,
  TIER_LABELS,
  tierRuns,
  type ClearanceTier,
  type LatLon,
  type Profile,
} from "./lineOfSight";
import { TERRAIN_ATTRIBUTION } from "./terrain";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";

const PATH_SOURCE = "los-path";
const HOVER_SOURCE = "los-hover";
const MOVED_SOURCE = "los-moved";
// Hovering within this many pixels of the path moves the profile cursor.
const HOVER_RADIUS_PX = 20;
// Each tier also has its own pattern, so the path reads without colour.
const TIER_DASHES: Record<ClearanceTier, number[] | null> = {
  clear: null,
  partial: [2, 1.5],
  blocked: [0.1, 2],
};

/**
 * A and B on the default basemap, with the path coloured by clearance. An end
 * moved to a site has a dashed line back to its reported position.
 */
export function LineOfSightMap({
  a,
  b,
  profile,
  hovered,
  onHover,
}: {
  a: LatLon;
  b: LatLon;
  profile: Profile;
  /** The hovered profile sample's index. */
  hovered: number | null;
  onHover: (index: number | null) => void;
}) {
  // Kept in refs so a new profile or handler does not rebuild the map.
  const samples = useRef(profile.samples);
  const hover = useRef(onHover);
  useEffect(() => {
    samples.current = profile.samples;
    hover.current = onHover;
  }, [profile, onHover]);

  const { latitude: aLat, longitude: aLon } = a;
  const { latitude: bLat, longitude: bLon } = b;
  const moved = [profile.a.search, profile.b.search].flatMap((search) =>
    search?.site ? [{ from: search.from, site: search.site }] : [],
  );
  const { container, map, failure } = useBasemapMap(
    {
      flat: true,
      attribution: TERRAIN_ATTRIBUTION,
      view: () => ({
        bounds: moved.reduce(
          (bounds, { from }) => bounds.extend([from.longitude, from.latitude]),
          new maplibregl.LngLatBounds([aLon, aLat], [aLon, aLat]).extend([
            bLon,
            bLat,
          ]),
        ),
        fitBoundsOptions: { padding: 40, maxZoom: 14 },
      }),
      onStyleLoad: addPathLayers,
      setup: (created) => {
        created.on("mousemove", (event) => {
          hover.current(nearestSample(created, samples.current, event.point));
        });
        created.on("mouseout", () => hover.current(null));
        for (const [label, lon, lat] of [
          ["A", aLon, aLat],
          ["B", bLon, bLat],
        ] as const) {
          const element = document.createElement("span");
          element.className = "minimap-marker minimap-target los-end-marker";
          element.textContent = label;
          new maplibregl.Marker({ element })
            .setLngLat([lon, lat])
            .addTo(created);
        }
      },
    },
    [aLat, aLon, bLat, bLon],
  );

  useEffect(() => {
    map?.getSource<maplibregl.GeoJSONSource>(PATH_SOURCE)?.setData({
      type: "FeatureCollection",
      features: tierRuns(profile.samples).map((run) => ({
        type: "Feature",
        properties: { tier: run.tier },
        geometry: {
          type: "LineString",
          coordinates: run.samples.map((sample) => [
            sample.longitude,
            sample.latitude,
          ]),
        },
      })),
    });
  }, [map, profile]);

  useEffect(() => {
    map?.getSource<maplibregl.GeoJSONSource>(MOVED_SOURCE)?.setData({
      type: "FeatureCollection",
      features: [profile.a.search, profile.b.search].flatMap((search) =>
        search?.site
          ? [
              {
                type: "Feature" as const,
                properties: {},
                geometry: {
                  type: "LineString" as const,
                  coordinates: [
                    [search.from.longitude, search.from.latitude],
                    [search.site.longitude, search.site.latitude],
                  ],
                },
              },
              {
                type: "Feature" as const,
                properties: {},
                geometry: {
                  type: "Point" as const,
                  coordinates: [search.from.longitude, search.from.latitude],
                },
              },
            ]
          : [],
      ),
    });
  }, [map, profile]);

  useEffect(() => {
    const sample = hovered === null ? undefined : profile.samples[hovered];
    map?.getSource<maplibregl.GeoJSONSource>(HOVER_SOURCE)?.setData({
      type: "FeatureCollection",
      features:
        sample === undefined
          ? []
          : [
              {
                type: "Feature",
                properties: {},
                geometry: {
                  type: "Point",
                  coordinates: [sample.longitude, sample.latitude],
                },
              },
            ],
    });
  }, [map, profile, hovered]);

  return (
    <figure className="los-map">
      {failure !== null ? (
        <p className="muted">{MAP_FAILURE_MESSAGES[failure]}</p>
      ) : null}
      <div
        ref={container}
        className="minimap-canvas"
        hidden={failure !== null}
      />
      <figcaption className="los-legend">
        {(Object.keys(TIER_LABELS) as ClearanceTier[]).map((tier) => (
          <span key={tier}>
            <svg width="28" height="8" aria-hidden="true">
              <line
                x1="2"
                x2="26"
                y1="4"
                y2="4"
                stroke={TIER_COLORS[tier]}
                strokeWidth="4"
                strokeLinecap={tier === "blocked" ? "round" : "butt"}
                strokeDasharray={TIER_DASHES[tier]
                  ?.map((dash) => dash * 4)
                  .join(" ")}
              />
            </svg>
            {TIER_LABELS[tier]}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}

function nearestSample(
  map: maplibregl.Map,
  samples: Profile["samples"],
  point: maplibregl.Point,
): number | null {
  let nearest: number | null = null;
  let best = HOVER_RADIUS_PX ** 2;
  samples.forEach((sample, index) => {
    const at = map.project([sample.longitude, sample.latitude]);
    const distance = (at.x - point.x) ** 2 + (at.y - point.y) ** 2;
    if (distance <= best) {
      best = distance;
      nearest = index;
    }
  });
  return nearest;
}

function addPathLayers(map: maplibregl.Map): void {
  map.addSource(PATH_SOURCE, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addSource(HOVER_SOURCE, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addSource(MOVED_SOURCE, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: "los-moved-line",
    type: "line",
    source: MOVED_SOURCE,
    filter: ["==", ["geometry-type"], "LineString"],
    paint: {
      "line-color": "#555555",
      "line-width": 2,
      "line-dasharray": [2, 2],
    },
  });
  map.addLayer({
    id: "los-moved-position",
    type: "circle",
    source: MOVED_SOURCE,
    filter: ["==", ["geometry-type"], "Point"],
    paint: {
      "circle-radius": 4,
      "circle-color": "#ffffff",
      "circle-stroke-color": "#555555",
      "circle-stroke-width": 2,
    },
  });
  for (const tier of Object.keys(TIER_DASHES) as ClearanceTier[]) {
    const dashes = TIER_DASHES[tier];
    map.addLayer({
      id: `los-${tier}`,
      type: "line",
      source: PATH_SOURCE,
      filter: ["==", ["get", "tier"], tier],
      layout: { "line-cap": tier === "blocked" ? "round" : "butt" },
      paint: {
        "line-color": TIER_COLORS[tier],
        "line-width": 4,
        ...(dashes === null ? {} : { "line-dasharray": dashes }),
      },
    });
  }
  map.addLayer({
    id: "los-hover",
    type: "circle",
    source: HOVER_SOURCE,
    paint: {
      "circle-radius": 6,
      "circle-color": "#ffd23f",
      "circle-stroke-color": "#111111",
      "circle-stroke-width": 2,
    },
  });
}
