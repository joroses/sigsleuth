import { useEffect, useMemo, useRef } from "react";
import * as maplibregl from "maplibre-gl";

import type { RelayCandidate, RelayCheckDetail } from "./api";
import { PLACED_BY, formatKm, formatNodeId } from "./nodeDisplay";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";

const CIRCLES = "relay-check-circles";
const ROUTE = "relay-check-route";
const MOVED = "relay-check-moved";
const EARTH_KM = 6371;

type Place = { latitude: number; longitude: number };

/**
 * One relay check on a map: the receiver and the co-hearers (squares), the
 * sender, every placed candidate coloured by what the rules made of it, the
 * route the copy took, and the rule-out (dashed) and naming (solid) reach
 * of the route's relay, the named ones and the selected candidate.
 */
export function RelayCheckMap({
  detail,
  selected,
  onSelect,
  onReceiver,
}: {
  detail: RelayCheckDetail;
  selected: number | null;
  onSelect: (key: number) => void;
  /** Moves the receiver to a clicked or dragged-to place. */
  onReceiver: (at: [number, number]) => void;
}) {
  // A ref, so a new handler does not rebuild the map.
  const moveReceiver = useRef(onReceiver);
  useEffect(() => {
    moveReceiver.current = onReceiver;
  }, [onReceiver]);
  const routeKey = detail.check.route_relay?.node_id ?? null;
  const named = useMemo(
    () =>
      new Set(
        [detail.check.page_relay, detail.check.rules_relay].flatMap((node) =>
          node?.node_id == null ? [] : [node.node_id],
        ),
      ),
    [detail],
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
          moveReceiver.current([event.lngLat.lat, event.lngLat.lng]);
        });
      },
    },
    [detail.check.reception_id],
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
        candidate.ruled_out.length > 0 ? "out" : "left",
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
          `relay-map-gateway receiver ${detail.receiver.placed_by}`,
          "R",
          `Receiver: ${PLACED_BY[detail.receiver.placed_by]}; drag to move`,
        ),
        true,
      );
      receiver.on("dragend", () => {
        const at = receiver.getLngLat();
        moveReceiver.current([at.lat, at.lng]);
      });
    }
    return () => markers.forEach((marker) => marker.remove());
  }, [map, detail, selected, onSelect, routeKey, named]);

  // Reach circles, the route and the sender's move.
  useEffect(() => {
    if (map === null) return;
    const colours = getComputedStyle(document.documentElement);
    const colour = (name: string) => colours.getPropertyValue(name).trim();
    const circled = detail.candidates.filter(
      (candidate) =>
        candidate.latitude !== null &&
        (candidate.key === routeKey ||
          named.has(candidate.key) ||
          candidate.key === selected),
    );
    map.getSource<maplibregl.GeoJSONSource>(CIRCLES)?.setData({
      type: "FeatureCollection",
      features: circled.flatMap((candidate) => {
        const tone =
          candidate.key === selected
            ? colour("--marker-ring")
            : candidate.key === routeKey
              ? colour("--blue")
              : colour("--accent");
        return [
          {
            km: Math.max(candidate.reach_km, detail.rules.rule_out_km),
            out: 1,
          },
          { km: Math.max(candidate.reach_km, detail.rules.name_km), out: 0 },
        ].map(({ km, out }) => ({
          type: "Feature" as const,
          properties: { tone, out },
          geometry: {
            type: "Polygon" as const,
            coordinates: [circle(candidate as Place, km)],
          },
        }));
      }),
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
  // A faint fill shows the reach when the whole view is inside it.
  map.addLayer({
    id: `${CIRCLES}-fill`,
    type: "fill",
    source: CIRCLES,
    filter: ["==", ["get", "out"], 0],
    paint: { "fill-color": ["get", "tone"], "fill-opacity": 0.06 },
  });
  map.addLayer({
    id: `${CIRCLES}-out`,
    type: "line",
    source: CIRCLES,
    filter: ["==", ["get", "out"], 1],
    paint: {
      "line-color": ["get", "tone"],
      "line-width": 1.5,
      "line-dasharray": [3, 2],
    },
  });
  map.addLayer({
    id: `${CIRCLES}-name`,
    type: "line",
    source: CIRCLES,
    filter: ["==", ["get", "out"], 0],
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
 * candidates that matter (left, the route's relay, the named ones). Ruled-out
 * candidates far away stay off it.
 */
function viewBounds(
  detail: RelayCheckDetail,
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
      (candidate.ruled_out.length === 0 ||
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

/** Drawn above: ruled out, then left, then named, then the route's relay. */
function layer(
  candidate: RelayCandidate,
  routeKey: number | null,
  named: Set<number>,
): number {
  if (candidate.key === routeKey) return 3;
  if (named.has(candidate.key)) return 2;
  return candidate.ruled_out.length === 0 ? 1 : 0;
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
    candidate.ruled_out.length === 0 ? "left" : candidate.ruled_out.join(", ");
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
