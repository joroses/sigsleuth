import { useEffect, useMemo, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";

import {
  type CoverageCellList,
  type CoverageExtent,
  type LinkTable,
  type NodeContactList,
  type NodeDetail,
  type NodePositionHistory,
  nodeContactsUrl,
  nodePositionsUrl,
} from "./api";
import {
  CONTACT_DAYS,
  type ContactLayers,
  type ContactMark,
  contactFacts,
  contactLayers,
} from "./nodeContacts";
import { now } from "./clock";
import {
  COVERAGE_FILL_LAYER,
  COVERAGE_SOURCE,
  type CoverageArea,
  addCoverageLayers,
  buildCoverageFeatureCollection,
  buildCoverageLines,
  cellText,
  cellsInView,
  coverageCellsUrl,
  coverageExtentUrl,
  nextCoverageArea,
  viewSpreadingFactor,
} from "./coverageLayer";
import {
  LINK_KINDS,
  LINK_LAYERS,
  ZERO_COST_LAYER,
  addLinkLayers,
  addZeroCostLayer,
  linkSourceId,
} from "./mapLinkLayers";
import { LinkSwatch, ZeroCostKey } from "./MapLinkDetails";
import {
  formatKm,
  nodeHref,
  nodeLabel,
  nodeMarkerLabel,
  radioName,
} from "./nodeDisplay";
import { spacedPositions } from "./nodePositionHistory";
import type { NodeRadios } from "./radios";
import { useLinkedSetting } from "./remember";
import { type NodeLinkView, nodeViewHref } from "./routes";
import { Icon, IconButton, ShareButton } from "./ui";
import { useApi } from "./useApi";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";

const HISTORY_SOURCE = "position-history";
const TRACK_LAYER = "position-track";
const POINTS_LAYER = "position-points";
// Position packets listed in a clicked spot's popup, newest first.
const POPUP_PACKETS = 5;

type Current = { latitude: number; longitude: number; estimated: boolean };

// Reported position history, contacts and survey coverage, with a pin at
// the current reported or estimated position. A shared node number's radios
// are in several places: a pin at each radio's latest sorted position
// instead, and no history or contact lines.
export function NodePositionMinimap({
  node,
  links,
  linkView = null,
  radios = null,
}: {
  node: NodeDetail;
  links: LinkTable | null;
  linkView?: NodeLinkView | null;
  radios?: NodeRadios | null;
}) {
  const { data: fetchedHistory, error } = useApi<NodePositionHistory>(
    radios === null ? nodePositionsUrl(node.node_id) : null,
  );
  const history = radios === null ? fetchedHistory : null;
  const places = useMemo(() => radioPlaces(radios), [radios]);
  const [showTraceroutes, setShowTraceroutes] = useLinkedSetting(
    "node.contacts.traceroutes",
    false,
    linkView?.show.includes("traceroutes"),
  );
  const [showGateways, setShowGateways] = useLinkedSetting(
    "node.contacts.gateways",
    false,
    linkView?.show.includes("gateways"),
  );
  const showContacts = radios === null && (showTraceroutes || showGateways);
  const [since] = useState(
    () => new Date(now() - CONTACT_DAYS * 24 * 60 * 60 * 1000),
  );
  const contactsState = useApi<NodeContactList>(
    showContacts ? nodeContactsUrl(node.node_id, since) : null,
  );
  const [showCoverage, setShowCoverage] = useLinkedSetting(
    "node.coverage",
    false,
    linkView?.show.includes("coverage"),
  );
  const [expanded, setExpanded] = useState(linkView?.expanded ?? false);
  useEffect(() => {
    if (!expanded) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [expanded]);
  const [coverageArea, setCoverageArea] = useState<CoverageArea | null>(null);
  const coverage = useApi<CoverageCellList>(
    showCoverage && coverageArea !== null
      ? coverageCellsUrl(coverageArea.fetched, node.node_id, null)
      : null,
  );
  const extent = useApi<CoverageExtent | null>(
    showCoverage ? coverageExtentUrl(node.node_id) : null,
  );
  // For the cell popup, which is set up once with the map.
  const spreadingFactor = useRef<number | null>(null);
  useEffect(() => {
    spreadingFactor.current =
      coverage.data === null || coverageArea === null
        ? null
        : viewSpreadingFactor(
            cellsInView(coverage.data, coverageArea.view).cells,
          );
  }, [coverage.data, coverageArea]);
  const loaded = radios !== null || history !== null || error !== null;
  const items = useMemo(() => spacedPositions(history?.items ?? []), [history]);
  const current: Current | null =
    radios !== null
      ? null
      : node.position !== null
        ? { ...node.position, estimated: false }
        : node.estimate !== null
          ? { ...node.estimate, estimated: true }
          : null;
  const contacts = useMemo(
    () =>
      showContacts && contactsState.data !== null
        ? contactLayers(
            node.node_id,
            contactsState.data.items,
            links?.items ?? [],
            links?.zero_cost_hops ?? [],
            current,
            { traceroutes: showTraceroutes, gateways: showGateways },
            contactsState.data.moved_at,
          )
        : null,
    // current is rebuilt each render; its place is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      showTraceroutes,
      showGateways,
      contactsState.data,
      links,
      node.node_id,
      current?.latitude,
      current?.longitude,
    ],
  );
  const hasMap =
    loaded &&
    (items.length > 0 ||
      current !== null ||
      places.length > 0 ||
      (contacts?.marks.length ?? 0) > 0 ||
      showCoverage);

  const { container, map, failure } = useBasemapMap(
    {
      enabled: hasMap,
      flat: true,
      // Contacts or coverage can place the view without a node position;
      // MapLibre cannot fit empty bounds while they load.
      view: () => {
        const bounds = historyBounds(items, current, places);
        return bounds.isEmpty()
          ? {}
          : { bounds, fitBoundsOptions: { padding: 40, maxZoom: 14 } };
      },
      onStyleLoad: (created, basemap) => {
        addHistoryLayers(created, items, basemap.darkLabels === false);
        // Under the contacts' lines and the history.
        addCoverageLayers(
          created,
          buildCoverageFeatureCollection(null),
          buildCoverageLines(null, [], new Map()),
          TRACK_LAYER,
        );
      },
      setup: (created) => {
        if (current !== null) {
          const element = document.createElement("span");
          element.className = `minimap-marker minimap-sender hop-tone-sender${
            current.estimated ? " estimated" : ""
          }`;
          element.title = current.estimated
            ? "Estimated from its neighbours"
            : "Current position";
          element.textContent = nodeMarkerLabel(node);
          // The pin's point is 6 px below its box (see .minimap-sender::after).
          new maplibregl.Marker({ element, anchor: "bottom", offset: [0, -6] })
            .setLngLat([current.longitude, current.latitude])
            .addTo(created);
        }
        for (const place of places) {
          const element = document.createElement("span");
          element.className = "minimap-marker minimap-sender hop-tone-sender";
          element.title = `${place.name}: latest position sorted to it`;
          element.textContent = place.label;
          new maplibregl.Marker({ element, anchor: "bottom", offset: [0, -6] })
            .setLngLat([place.longitude, place.latitude])
            .addTo(created);
        }
        const popup = new maplibregl.Popup({
          className: "history-popup",
          maxWidth: "none",
        });
        created.on("click", POINTS_LAYER, (event) => {
          popup
            .setLngLat(event.lngLat)
            .setDOMContent(popupContent(event.features ?? []))
            .addTo(created);
        });
        created.on("mouseenter", POINTS_LAYER, () => {
          created.getCanvas().style.cursor = "pointer";
        });
        created.on("mouseleave", POINTS_LAYER, () => {
          created.getCanvas().style.cursor = "";
        });
        const cellPopup = new maplibregl.Popup({
          closeButton: false,
          closeOnClick: false,
          className: "coverage-popup",
          offset: 24,
        });
        created.on("mousemove", COVERAGE_FILL_LAYER, (event) => {
          const cell = event.features?.[0]?.properties;
          if (cell === undefined) return;
          cellPopup
            .setLngLat(event.lngLat)
            .setText(cellText(cell, spreadingFactor.current))
            .addTo(created);
        });
        created.on("mouseleave", COVERAGE_FILL_LAYER, () => cellPopup.remove());
        const updateCoverageArea = () => {
          const bounds = created.getBounds();
          setCoverageArea((area) =>
            nextCoverageArea(area, {
              west: bounds.getWest(),
              south: bounds.getSouth(),
              east: bounds.getEast(),
              north: bounds.getNorth(),
              zoom: created.getZoom(),
            }),
          );
        };
        created.on("moveend", updateCoverageArea);
        updateCoverageArea();
      },
    },
    [hasMap, items, places, current?.latitude, current?.longitude],
  );

  useEffect(() => {
    if (map === null || contacts === null) return;
    return drawContacts(map, contacts);
  }, [map, contacts]);

  // One fit for everything shown: the history and pin, and the contacts and
  // coverage cells when on, so the widest sets the view whichever loads
  // last. It waits for both, and fits again when one is turned on or off;
  // the cells for the new view follow (moveend).
  const fit = showCoverage ? extent.data : null;
  const fitWaits =
    (showContacts && contactsState.loading) || (showCoverage && extent.loading);
  useEffect(() => {
    if (map === null || fitWaits) return;
    const bounds = historyBounds(items, current, places);
    for (const mark of contacts?.marks ?? []) {
      bounds.extend([mark.longitude, mark.latitude]);
    }
    if (fit !== null) {
      bounds.extend([fit.west, fit.south]);
      bounds.extend([fit.east, fit.north]);
    }
    if (!bounds.isEmpty()) map.fitBounds(bounds, { padding: 40, maxZoom: 14 });
    // current is rebuilt each render and contacts follow its place; the
    // extent's values count, not its object, which a refetch replaces.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    map,
    fitWaits,
    items,
    places,
    contacts,
    fit?.west,
    fit?.south,
    fit?.east,
    fit?.north,
  ]);

  // The cells shown stay until the next ones arrive.
  useEffect(() => {
    if (map === null || (showCoverage && coverage.data === null)) return;
    const source = map.getSource(COVERAGE_SOURCE) as
      maplibregl.GeoJSONSource | undefined;
    source?.setData(
      buildCoverageFeatureCollection(showCoverage ? coverage.data : null),
    );
  }, [map, showCoverage, coverage.data]);

  return (
    <>
      {expanded ? (
        <div
          className="node-position-backdrop"
          onClick={() => setExpanded(false)}
        />
      ) : null}
      <figure
        className={`node-position ${expanded ? "node-position-expanded" : ""}`}
      >
        <div className="node-position-head">
          <div
            className="minimap-toggles"
            role="group"
            aria-label="Show on the map"
          >
            {radios !== null ? null : (
              <>
                <button
                  type="button"
                  aria-pressed={showTraceroutes}
                  title={`Traceroute neighbours, last ${CONTACT_DAYS} days`}
                  onClick={() => setShowTraceroutes(!showTraceroutes)}
                >
                  <Icon name="route" />
                  Traceroutes
                </button>
                <button
                  type="button"
                  aria-pressed={showGateways}
                  title={`Gateways that heard it, last ${CONTACT_DAYS} days`}
                  onClick={() => setShowGateways(!showGateways)}
                >
                  <Icon name="radio" />
                  Gateways
                </button>
              </>
            )}
            <button
              type="button"
              aria-pressed={showCoverage}
              title="Where survey nodes heard it"
              onClick={() => setShowCoverage(!showCoverage)}
            >
              <Icon name="coverage" />
              Coverage
            </button>
          </div>
          <div className="minimap-actions">
            <IconButton
              icon={expanded ? "close" : "maximize"}
              label={expanded ? "Close the bigger map (Esc)" : "Expand the map"}
              aria-pressed={expanded}
              onClick={() => setExpanded(!expanded)}
            />
            <ShareButton
              hash={nodeViewHref(node.node_id, {
                expanded,
                show: [
                  ...(showTraceroutes ? (["traceroutes"] as const) : []),
                  ...(showGateways ? (["gateways"] as const) : []),
                  ...(showCoverage ? (["coverage"] as const) : []),
                ],
              })}
              title={nodeLabel(node)}
            />
          </div>
        </div>
        {/* Always there, so the card keeps its shape while loading or with
          nothing to draw; the map fills what the notes leave. */}
        <div className="node-position-map">
          {failure !== null ? (
            <p className="muted">{MAP_FAILURE_MESSAGES[failure]}</p>
          ) : null}
          {hasMap ? (
            <div
              ref={container}
              className="minimap-canvas"
              hidden={failure !== null}
            />
          ) : null}
          {contactsState.data !== null && (contacts?.marks.length ?? 0) > 0 ? (
            <ContactsKey
              traceroutes={showTraceroutes}
              gateways={showGateways}
              zeroCost={(contacts?.zeroCost.features.length ?? 0) > 0}
            />
          ) : null}
        </div>
        <figcaption className="muted">
          <span className="data-line">
            {radios === null
              ? historyNote(history, error, items.length)
              : `${places.length.toLocaleString()} of ${radios.items.length.toLocaleString()} radios placed, each at its latest sorted position`}
          </span>
          {showContacts ? (
            <ContactsNote state={contactsState} contacts={contacts} />
          ) : null}
          {showCoverage && hasMap ? (
            <span className="data-line">
              {coverageNote(coverage, coverageArea, extent)}
            </span>
          ) : null}
        </figcaption>
      </figure>
    </>
  );
}

// The contacts' count on a line.
function ContactsNote({
  state,
  contacts,
}: {
  state: { data: NodeContactList | null; error: string | null };
  contacts: ContactLayers | null;
}) {
  if (state.error !== null)
    return (
      <span className="data-line">Contacts could not load: {state.error}</span>
    );
  if (state.data === null || contacts === null)
    return <span className="data-line">Loading contacts…</span>;
  if (state.data.items.length === 0)
    return (
      <span className="data-line">
        No contacts in the last {CONTACT_DAYS} days.
      </span>
    );
  const unplaced =
    contacts.unplaced > 0
      ? ` · ${contacts.unplaced.toLocaleString()} unplaced`
      : "";
  const moved =
    contacts.moved > 0
      ? ` · ${contacts.moved.toLocaleString()} left out after a move`
      : "";
  return (
    <span className="data-line">
      Last {CONTACT_DAYS} days: {contacts.marks.length.toLocaleString()}{" "}
      contacts drawn{unplaced}
      {moved} · click one for its distances
    </span>
  );
}

// A key to the contacts' lines and numbers, in the map's corner: the Map's
// link patterns.
function ContactsKey({
  traceroutes,
  gateways,
  zeroCost,
}: {
  traceroutes: boolean;
  gateways: boolean;
  zeroCost: boolean;
}) {
  return (
    <ul className="coverage-line-legend minimap-key" aria-label="Key">
      {traceroutes ? (
        <li>
          <LinkSwatch tier={null} linkKind="traceroute" />
          traceroute hop
        </li>
      ) : null}
      {traceroutes ? (
        <li>
          <span className="contacts-legend-hops hop-tone-relay" />
          Relay
        </li>
      ) : null}
      {zeroCost ? <ZeroCostKey /> : null}
      {gateways ? (
        <>
          <li title="Heard directly by a gateway">
            <LinkSwatch tier={null} linkKind="direct" />
            heard directly
          </li>
          <li>
            <LinkSwatch tier={null} linkKind="neighbor" />
            neighbour
          </li>
        </>
      ) : null}
      {gateways ? (
        <li title="Fewest hops to a gateway">
          <span className="contacts-legend-hops hop-tone-1">1</span>
          Gateway: hops away
        </li>
      ) : null}
    </ul>
  );
}

function coverageNote(
  state: { data: CoverageCellList | null; error: string | null },
  area: CoverageArea | null,
  extent: { data: CoverageExtent | null; loading: boolean },
): string {
  if (state.error !== null) return `Coverage could not load: ${state.error}`;
  if (!extent.loading && extent.data === null)
    return "No survey data associated with this node.";
  if (state.data === null || area === null) return "Loading coverage…";
  let measurements = 0;
  let guessed = 0;
  for (const cell of cellsInView(state.data, area.view).cells) {
    measurements += cell.measurements;
    guessed += cell.guessed;
  }
  if (measurements === 0) return "No survey measurement in view; zoom out.";
  const counts = `${measurements.toLocaleString()} survey ${measurements === 1 ? "measurement" : "measurements"} in view · ${guessed.toLocaleString()} identified using MQTT evidence`;
  return state.data.truncated
    ? `${counts} · busiest cells only, zoom in`
    : counts;
}

// The links to the contacts, under the position history, and a marker on
// every placed contact. Returns the cleanup.
function drawContacts(
  map: maplibregl.Map,
  contacts: ContactLayers,
): () => void {
  // Shown at every zoom: the minimap starts wide.
  for (const kind of LINK_KINDS) {
    addLinkLayers(map, kind, contacts.lines[kind], TRACK_LAYER, 0);
  }
  addZeroCostLayer(map, contacts.zeroCost, TRACK_LAYER, 0);
  const popup = new maplibregl.Popup({
    className: "history-popup",
    maxWidth: "none",
  });
  const markers = contacts.marks.map((mark) => {
    const element = document.createElement("button");
    element.type = "button";
    element.className = `minimap-marker minimap-relay ${mark.tone}${mark.estimated ? " estimated" : ""}`;
    element.title = nodeLabel({
      node_id: mark.contact.node_id,
      long_name: mark.contact.node?.long_name ?? null,
      short_name: mark.contact.node?.short_name ?? null,
    });
    element.textContent = mark.label;
    const marker = new maplibregl.Marker({ element })
      .setLngLat([mark.longitude, mark.latitude])
      .addTo(map);
    element.addEventListener("click", (event) => {
      event.stopPropagation();
      popup
        .setLngLat([mark.longitude, mark.latitude])
        .setDOMContent(contactContent(mark))
        .addTo(map);
    });
    return marker;
  });
  return () => {
    popup.remove();
    for (const marker of markers) marker.remove();
    for (const layer of [...LINK_LAYERS, ZERO_COST_LAYER]) {
      if (map.getLayer(layer)) map.removeLayer(layer);
    }
    for (const source of [...LINK_KINDS.map(linkSourceId), ZERO_COST_LAYER]) {
      if (map.getSource(source)) map.removeSource(source);
    }
  };
}

// A clicked contact: its name linked to its page, how far it is now, and
// what ties it to the node.
function contactContent(mark: ContactMark): HTMLElement {
  const { contact } = mark;
  const content = document.createElement("div");
  const link = document.createElement("a");
  link.href = nodeHref(contact.node_id);
  link.textContent = nodeLabel({
    node_id: contact.node_id,
    long_name: contact.node?.long_name ?? null,
    short_name: contact.node?.short_name ?? null,
  });
  const title = document.createElement("strong");
  title.append(link);
  content.append(title);
  const list = document.createElement("ul");
  const facts = [
    mark.estimated
      ? "Drawn at an estimate from its neighbours"
      : contact.km === null
        ? "Distance now unknown"
        : `${formatKm(contact.km)} km away now`,
    ...contactFacts(contact),
  ];
  for (const fact of facts) {
    const item = document.createElement("li");
    item.textContent = fact;
    list.append(item);
  }
  content.append(list);
  return content;
}

function historyNote(
  history: NodePositionHistory | null,
  error: string | null,
  shown: number,
): string {
  if (error !== null) return `Position history could not load: ${error}`;
  if (history === null) return "Loading position history…";
  const { items, invalid, limited } = history;
  const count =
    items.length === 0
      ? "No valid position packets of its own."
      : `${shown.toLocaleString()} ${shown === 1 ? "position" : "positions"}${shown < items.length ? " · 3 km spacing" : ""} · click a dot for its packet`;
  const skipped = invalid > 0 ? ` · ${invalid.toLocaleString()} invalid` : "";
  const older = limited ? " · older not drawn" : "";
  return count + skipped + older;
}

function historyBounds(
  items: NodePositionHistory["items"],
  current: Current | null,
  places: readonly RadioPlace[],
): maplibregl.LngLatBounds {
  const bounds = new maplibregl.LngLatBounds();
  for (const item of items) bounds.extend([item.longitude, item.latitude]);
  if (current !== null) bounds.extend([current.longitude, current.latitude]);
  for (const place of places) bounds.extend([place.longitude, place.latitude]);
  return bounds;
}

type RadioPlace = {
  name: string;
  label: string;
  latitude: number;
  longitude: number;
};

const NO_PLACES: RadioPlace[] = [];

function radioPlaces(radios: NodeRadios | null): RadioPlace[] {
  if (radios === null) return NO_PLACES;
  return radios.items.flatMap((radio) =>
    radio.position === null
      ? []
      : [
          {
            name: radioName(radio),
            label: radio.short_name ?? String(radio.number),
            latitude: radio.position.latitude,
            longitude: radio.position.longitude,
          },
        ],
  );
}

// A line through the positions in time order and a dot at each, newer dots
// stronger and drawn on top.
function addHistoryLayers(
  map: maplibregl.Map,
  items: NodePositionHistory["items"],
  darkBasemap: boolean,
): void {
  const last = Math.max(1, items.length - 1);
  const features: GeoJSON.Feature[] = items.map((item, index) => ({
    type: "Feature",
    geometry: { type: "Point", coordinates: [item.longitude, item.latitude] },
    properties: {
      packetRowId: item.packet_row_id,
      observedAt: item.observed_at,
      newness: 1 - index / last,
    },
  }));
  if (items.length > 1) {
    features.push({
      type: "Feature",
      geometry: {
        type: "LineString",
        coordinates: items.map((item) => [item.longitude, item.latitude]),
      },
      properties: {},
    });
  }
  map.addSource(HISTORY_SOURCE, {
    type: "geojson",
    data: { type: "FeatureCollection", features },
  });
  const colour = "#3987e5";
  map.addLayer({
    id: "position-track",
    type: "line",
    source: HISTORY_SOURCE,
    filter: ["==", ["geometry-type"], "LineString"],
    paint: { "line-color": colour, "line-width": 1.5, "line-opacity": 0.5 },
  });
  map.addLayer({
    id: POINTS_LAYER,
    type: "circle",
    source: HISTORY_SOURCE,
    filter: ["==", ["geometry-type"], "Point"],
    layout: { "circle-sort-key": ["get", "newness"] },
    paint: {
      "circle-color": colour,
      "circle-radius": 5,
      "circle-opacity": [
        "interpolate",
        ["linear"],
        ["get", "newness"],
        0,
        0.3,
        1,
        1,
      ],
      "circle-stroke-color": darkBasemap ? "#0f172a" : "#ffffff",
      "circle-stroke-width": 1,
    },
  });
}

// The position packets under a click, newest first, each with its date
// linked to its packet page when it has one.
function popupContent(features: maplibregl.MapGeoJSONFeature[]): HTMLElement {
  const packets = features
    .map((feature) => ({
      // Map features may drop a null property.
      id:
        typeof feature.properties.packetRowId === "number"
          ? feature.properties.packetRowId
          : null,
      at: String(feature.properties.observedAt),
    }))
    .sort((a, b) => b.at.localeCompare(a.at));
  const content = document.createElement("div");
  const list = document.createElement("ul");
  for (const packet of packets.slice(0, POPUP_PACKETS)) {
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.dateTime = packet.at;
    const date = new Date(packet.at);
    time.title = date.toLocaleString();
    time.textContent = date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
    item.append(time);
    if (packet.id !== null) {
      const link = document.createElement("a");
      link.href = `#packets/${packet.id}`;
      link.textContent = "Position packet";
      item.append(" · ", link);
    }
    list.append(item);
  }
  content.append(list);
  if (packets.length > POPUP_PACKETS) {
    const more = document.createElement("p");
    const older = packets.length - POPUP_PACKETS;
    more.textContent = `and ${older} older nearby`;
    content.append(more);
  }
  return content;
}
