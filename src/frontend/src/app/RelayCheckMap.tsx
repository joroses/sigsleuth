import { useEffect, useMemo, useRef } from "react";
import * as maplibregl from "maplibre-gl";

import type { RelayCandidate, RelayCheckDetail } from "./api";

/** What the map draws: a relay check's or a survey measurement's copy. */
export type MapCopy = Pick<
  RelayCheckDetail,
  | "candidates"
  | "reports"
  | "receiver"
  | "sender"
  | "sender_when_heard"
  | "route_places"
  | "rules"
>;
import { PLACED_BY, formatKm, formatNodeId } from "./nodeDisplay";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";

const CIRCLES = "relay-check-circles";
const ROUTE = "relay-check-route";
const MOVED = "relay-check-moved";
const EARTH_KM = 6371;

type Place = { latitude: number; longitude: number };

/**
 * One relay check on a map: the receiver with its near-links radius and the
 * co-hearers (squares), the sender, every placed candidate coloured by whether
 * any evidence ties it to the copy, the route the copy took, and the selected
 * candidate's reach when clicked.
 */
export function RelayCheckMap({
  detail,
  viewKey,
  routeKey,
  namedKeys,
  selected,
  onSelect,
  onReceiver,
}: {
  detail: MapCopy;
  /** The copy shown: the view is fitted again when it changes. */
  viewKey: number | string;
  /** The route's relay, drawn blue. */
  routeKey: number | null;
  /** The relays named, drawn green. */
  namedKeys: number[];
  selected: number | null;
  onSelect: (key: number) => void;
  /** Moves the receiver to a clicked or dragged-to place; none keeps it. */
  onReceiver?: (at: [number, number]) => void;
}) {
  // A ref, so a new handler does not rebuild the map.
  const moveReceiver = useRef(onReceiver);
  useEffect(() => {
    moveReceiver.current = onReceiver;
  }, [onReceiver]);
  const movable = onReceiver !== undefined;
  const namedKey = namedKeys.join(",");
  const named = useMemo(
    () => new Set(namedKey === "" ? [] : namedKey.split(",").map(Number)),
    [namedKey],
  );
  const { container, map, failure } = useBasemapMap(
    {
      flat: true,
      view: () => ({
        bounds: viewBounds(detail, routeKey, named),
        fitBoundsOptions: { padding: 40, maxZoom: 10 },
      }),
      onStyleLoad: addLayers,
      setup: (created) => {
        created.on("click", (event) => {
          // A click on a marker selects it instead.
          const target = event.originalEvent.target as HTMLElement | null;
          if (target?.closest(".maplibregl-marker")) return;
          moveReceiver.current?.([event.lngLat.lat, event.lngLat.lng]);
        });
      },
    },
    [viewKey],
  );

  // Markers, redrawn when the selection changes.
  useEffect(() => {
    if (map === null) return;
    const markers: maplibregl.Marker[] = [];
    const add = (place: Place, element: HTMLElement, draggable = false) => {
      const marker = new maplibregl.Marker({ element, draggable })
        .setLngLat([place.longitude, place.latitude])
        .addTo(map);
      markers.push(marker);
      return marker;
    };
    const sorted = [...detail.candidates].sort(
      (a, b) => layer(a, routeKey, named) - layer(b, routeKey, named),
    );
    for (const candidate of sorted) {
      if (candidate.latitude === null || candidate.longitude === null) continue;
      const element = document.createElement("button");
      element.type = "button";
      element.className = [
        "relay-map-node",
        candidate.evidence ? "left" : "out",
        named.has(candidate.key) ? "named" : "",
        candidate.key === routeKey ? "route" : "",
        candidate.key === selected ? "selected" : "",
        candidate.estimated ? "estimated" : "",
      ].join(" ");
      element.textContent = shortName(candidate);
      element.title = candidateTitle(candidate);
      element.addEventListener("click", (event) => {
        event.stopPropagation();
        onSelect(candidate.key);
      });
      add(
        { latitude: candidate.latitude, longitude: candidate.longitude },
        element,
      );
    }
    for (const report of detail.reports) {
      if (
        report.kind !== "cohearer" ||
        report.latitude === null ||
        report.longitude === null
      )
        continue;
      add(
        { latitude: report.latitude, longitude: report.longitude },
        label("relay-map-gateway cohearer", "C", "Co-hearer"),
      );
    }
    if (detail.sender_when_heard !== null) {
      add(
        detail.sender_when_heard,
        label("relay-map-sender heard", "S", "Sender when heard"),
      );
    }
    if (detail.sender !== null) {
      add(
        detail.sender,
        label(
          `relay-map-sender ${detail.sender.placed_by}`,
          "S",
          `Sender as the rules place it: ${PLACED_BY[detail.sender.placed_by]}`,
        ),
      );
    }
    if (detail.receiver !== null) {
      const receiver = add(
        detail.receiver,
        label(
          `relay-map-gateway receiver ${detail.receiver.placed_by}${movable ? " movable" : ""}`,
          "R",
          movable
            ? `Receiver: ${PLACED_BY[detail.receiver.placed_by]}, drag to move`
            : `Receiver: ${PLACED_BY[detail.receiver.placed_by]}`,
        ),
        movable,
      );
      receiver.on("dragend", () => {
        const at = receiver.getLngLat();
        moveReceiver.current?.([at.lat, at.lng]);
      });
    }
    return () => markers.forEach((marker) => marker.remove());
  }, [map, detail, selected, onSelect, routeKey, named, movable]);

  useEffect(() => {
    if (map === null) return;
    const colours = getComputedStyle(document.documentElement);
    const colour = (name: string) => colours.getPropertyValue(name).trim();
    const ring = colour("--marker-ring");
    const circleFeatures = [];
    if (detail.receiver !== null) {
      circleFeatures.push({
        type: "Feature" as const,
        properties: { tone: ring, kind: "near" },
        geometry: {
          type: "Polygon" as const,
          coordinates: [circle(detail.receiver, detail.rules.near_km)],
        },
      });
    }
    const picked =
      selected === null
        ? undefined
        : detail.candidates.find(
            (candidate) =>
              candidate.key === selected &&
              candidate.latitude !== null &&
              candidate.longitude !== null,
          );
    if (picked !== undefined) {
      circleFeatures.push({
        type: "Feature" as const,
        properties: { tone: ring, kind: "reach" },
        geometry: {
          type: "Polygon" as const,
          coordinates: [
            circle(
              picked as Place,
              Math.max(picked.reach_km, detail.rules.name_km),
            ),
          ],
        },
      });
    }
    map.getSource<maplibregl.GeoJSONSource>(CIRCLES)?.setData({
      type: "FeatureCollection",
      features: circleFeatures,
    });
    map.getSource<maplibregl.GeoJSONSource>(ROUTE)?.setData({
      type: "FeatureCollection",
      features:
        detail.route_places.length < 2
          ? []
          : [
              {
                type: "Feature",
                properties: { tone: colour("--blue") },
                geometry: {
                  type: "LineString",
                  coordinates: detail.route_places.map((place) => [
                    place.longitude,
                    place.latitude,
                  ]),
                },
              },
            ],
    });
    const moved =
      detail.sender !== null && detail.sender_when_heard !== null
        ? [detail.sender_when_heard, detail.sender]
        : [];
    map.getSource<maplibregl.GeoJSONSource>(MOVED)?.setData({
      type: "FeatureCollection",
      features:
        moved.length === 2
          ? [
              {
                type: "Feature",
                properties: { tone: colour("--amber") },
                geometry: {
                  type: "LineString",
                  coordinates: moved.map((place) => [
                    place.longitude,
                    place.latitude,
                  ]),
                },
              },
            ]
          : [],
    });
  }, [map, detail, selected, routeKey, named]);

  return (
    <div className="relay-check-map">
      <div ref={container} className="relay-check-map-canvas" />
      {failure !== null ? (
        <p className="relay-check-map-failure">
          {MAP_FAILURE_MESSAGES[failure]}
        </p>
      ) : null}
    </div>
  );
}

function addLayers(map: maplibregl.Map) {
  for (const id of [CIRCLES, ROUTE, MOVED]) {
    map.addSource(id, {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
    });
  }
  map.addLayer({
    id: `${CIRCLES}-near-fill`,
    type: "fill",
    source: CIRCLES,
    filter: ["==", ["get", "kind"], "near"],
    paint: { "fill-color": ["get", "tone"], "fill-opacity": 0.04 },
  });
  map.addLayer({
    id: `${CIRCLES}-near-line`,
    type: "line",
    source: CIRCLES,
    filter: ["==", ["get", "kind"], "near"],
    paint: {
      "line-color": ["get", "tone"],
      "line-width": 1.5,
      "line-opacity": 0.45,
      "line-dasharray": [4, 3],
    },
  });
  map.addLayer({
    id: `${CIRCLES}-reach`,
    type: "line",
    source: CIRCLES,
    filter: ["==", ["get", "kind"], "reach"],
    paint: { "line-color": ["get", "tone"], "line-width": 2 },
  });
  map.addLayer({
    id: ROUTE,
    type: "line",
    source: ROUTE,
    paint: {
      "line-color": ["get", "tone"],
      "line-width": 3,
      "line-opacity": 0.8,
    },
  });
  map.addLayer({
    id: MOVED,
    type: "line",
    source: MOVED,
    paint: {
      "line-color": ["get", "tone"],
      "line-width": 2,
      "line-dasharray": [1, 2],
    },
  });
}

/**
 * What the view first shows: the receiver, the sender, the route and the
 * candidates that matter (with evidence, the route's relay, the named ones).
 * Others far away stay off it.
 */
function viewBounds(
  detail: MapCopy,
  routeKey: number | null,
  named: Set<number>,
): maplibregl.LngLatBounds {
  const places: Place[] = [
    ...[detail.receiver, detail.sender, detail.sender_when_heard].flatMap(
      (place) => (place === null ? [] : [place]),
    ),
    ...detail.route_places,
    ...detail.candidates.flatMap((candidate) =>
      candidate.latitude !== null &&
      candidate.longitude !== null &&
      (candidate.evidence ||
        candidate.key === routeKey ||
        named.has(candidate.key))
        ? [{ latitude: candidate.latitude, longitude: candidate.longitude }]
        : [],
    ),
  ];
  const first = places[0] ?? { latitude: 0, longitude: 0 };
  return places.reduce(
    (bounds, place) => bounds.extend([place.longitude, place.latitude]),
    new maplibregl.LngLatBounds(
      [first.longitude, first.latitude],
      [first.longitude, first.latitude],
    ),
  );
}

/** Drawn above: without evidence, then with, then named, then the route's relay. */
function layer(
  candidate: RelayCandidate,
  routeKey: number | null,
  named: Set<number>,
): number {
  if (candidate.key === routeKey) return 3;
  if (named.has(candidate.key)) return 2;
  return candidate.evidence ? 1 : 0;
}

function label(className: string, text: string, title: string): HTMLElement {
  const element = document.createElement("span");
  element.className = className;
  element.textContent = text;
  element.title = title;
  return element;
}

function shortName(candidate: RelayCandidate): string {
  if (candidate.short_name !== null) return candidate.short_name;
  return candidate.node_id === null
    ? "?"
    : formatNodeId(candidate.node_id).slice(-4);
}

function candidateTitle(candidate: RelayCandidate): string {
  const name =
    candidate.name ??
    candidate.short_name ??
    (candidate.node_id === null
      ? "An opted-out node"
      : formatNodeId(candidate.node_id));
  const state =
    candidate.probability === null
      ? "cannot have sent it"
      : `${Math.round(candidate.probability * 100)}%`;
  return `${name}: ${state}, reach ${formatKm(candidate.reach_km)} km`;
}

/** A circle of `km` around a place, as a closed line of lon/lat points. */
function circle(centre: Place, km: number, points = 96): number[][] {
  const distance = km / EARTH_KM;
  const lat = (centre.latitude * Math.PI) / 180;
  const lon = (centre.longitude * Math.PI) / 180;
  const ring: number[][] = [];
  for (let index = 0; index <= points; index += 1) {
    const bearing = (2 * Math.PI * index) / points;
    const pointLat = Math.asin(
      Math.sin(lat) * Math.cos(distance) +
        Math.cos(lat) * Math.sin(distance) * Math.cos(bearing),
    );
    const pointLon =
      lon +
      Math.atan2(
        Math.sin(bearing) * Math.sin(distance) * Math.cos(lat),
        Math.cos(distance) - Math.sin(lat) * Math.sin(pointLat),
      );
    ring.push([(pointLon * 180) / Math.PI, (pointLat * 180) / Math.PI]);
  }
  return ring;
}
