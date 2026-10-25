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
import { LINK_TIERS } from "./linkQuality";
import {
  LINK_KINDS,
  LINK_LAYERS,
  addLinkLayers,
  linkSourceId,
} from "./mapLinkLayers";
import { LinkSwatch } from "./MapLinkDetails";
import { formatKm, nodeHref, nodeLabel, nodeMarkerLabel } from "./nodeDisplay";
import { useSetting } from "./remember";
import { Icon } from "./ui";
import { useApi } from "./useApi";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";

const HISTORY_SOURCE = "position-history";
const TRACK_LAYER = "position-track";
const POINTS_LAYER = "position-points";
// Position packets listed in a clicked spot's popup, newest first.
const POPUP_PACKETS = 5;

type Current = { latitude: number; longitude: number; estimated: boolean };

// Where the node's own position packets placed it, as dots joined in time
// order (newer dots stronger), with a pin at its current position: reported,
// from its map report, or estimated (hollow). Clicking a dot lists the
// position packets sent from that spot, with their dates and links.
//
// "Contacts" adds everything that tells where the node is over the last
// CONTACT_DAYS, with no map rule hiding any: its links drawn as on the Map, to
// each placed node linked with it (its Links table's rows, `links`), and a
// marker on each gateway that heard its packets (contactLayers).
// Clicking a contact lists its links' distances when heard, so a link
// measured from a wrong position stands out by its range.
//
// "Coverage" adds the signal survey cells where survey nodes heard it,
// its own packets or ones it relayed, as on the Map's "Heard from it"; not
// what its own survey uploaded. Cells are fetched for the area in view.
//
// The view fits all that is shown, with every contact and every cell.
export function NodePositionMinimap({
  node,
  links,
}: {
  node: NodeDetail;
  links: LinkTable | null;
}) {
  const { data: history, error } = useApi<NodePositionHistory>(
    nodePositionsUrl(node.node_id),
  );
  const [showTraceroutes, setShowTraceroutes] = useSetting(
    "node.contacts.traceroutes",
    false,
  );
  const [showGateways, setShowGateways] = useSetting(
    "node.contacts.gateways",
    false,
  );
  const showContacts = showTraceroutes || showGateways;
  const [since] = useState(
    () => new Date(now() - CONTACT_DAYS * 24 * 60 * 60 * 1000),
  );
  const contactsState = useApi<NodeContactList>(
    showContacts ? nodeContactsUrl(node.node_id, since) : null,
  );
  const [showCoverage, setShowCoverage] = useSetting("node.coverage", false);
  const [expanded, setExpanded] = useState(false);
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
  const loaded = history !== null || error !== null;
  const items = useMemo(() => history?.items ?? [], [history]);
  const current: Current | null =
    node.position !== null
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
            current,
            { traceroutes: showTraceroutes, gateways: showGateways },
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
      (contacts?.marks.length ?? 0) > 0 ||
      showCoverage);

  const { container, map, failure } = useBasemapMap(
    {
      enabled: hasMap,
      flat: true,
      // Contacts or coverage can place the view without a node position;
      // MapLibre cannot fit empty bounds while they load.
      view: () => {
        const bounds = historyBounds(items, current);
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
    [hasMap, items, current?.latitude, current?.longitude],
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
    const bounds = historyBounds(items, current);
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
          <h3>Position</h3>
          <div
            className="minimap-toggles"
            role="group"
            aria-label="Show on the map"
          >
            <button
              type="button"
              aria-pressed={showTraceroutes}
              title={`Nodes in its traceroutes over the last ${CONTACT_DAYS} days, with no map rule hiding any`}
              onClick={() => setShowTraceroutes(!showTraceroutes)}
            >
              <Icon name="route" />
              Traceroutes
              <span className="minimap-toggle-hint">{CONTACT_DAYS} d</span>
            </button>
            <button
              type="button"
              aria-pressed={showGateways}
              title={`Gateways that heard it and nodes heard with it directly or as neighbours over the last ${CONTACT_DAYS} days, with no map rule hiding any`}
              onClick={() => setShowGateways(!showGateways)}
            >
              <Icon name="radio" />
              Gateways
              <span className="minimap-toggle-hint">{CONTACT_DAYS} d</span>
            </button>
            <button
              type="button"
              aria-pressed={showCoverage}
              title="Where survey nodes heard it, its own packets or ones it relayed"
              onClick={() => setShowCoverage(!showCoverage)}
            >
              <Icon name="coverage" />
              Coverage
            </button>
            <button
              type="button"
              aria-pressed={expanded}
              title={expanded ? "Back to the page (Esc)" : "A bigger map"}
              onClick={() => setExpanded(!expanded)}
            >
              <Icon name={expanded ? "collapse" : "expand"} />
              {expanded ? "Close" : "Expand"}
            </button>
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
              contacts={contactsState.data}
              traceroutes={showTraceroutes}
              gateways={showGateways}
            />
          ) : null}
        </div>
        <figcaption className="muted">
          <span className="data-line">{historyNote(history, error)}</span>
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
  if (state.data.items.length === 0) return null;
  const unplaced =
    contacts.unplaced > 0
      ? ` · ${contacts.unplaced.toLocaleString()} unplaced`
      : "";
  return (
    <span className="data-line">
      {contacts.marks.length.toLocaleString()} contacts drawn{unplaced} · click
      one for its distances
    </span>
  );
}

// A key to the contacts' lines and numbers, in the map's corner: the Map's
// link patterns and colours.
function ContactsKey({
  contacts,
  traceroutes,
  gateways,
}: {
  contacts: NodeContactList;
  traceroutes: boolean;
  gateways: boolean;
}) {
  return (
    <ul className="coverage-line-legend minimap-key" aria-label="Key">
      {traceroutes ? (
        <li>
          <LinkSwatch tier={null} linkKind="traceroute" />
          traceroute
        </li>
      ) : null}
      {gateways ? (
        <>
          <li title="A gateway heard it with no relay, or it heard the gateway">
            <LinkSwatch tier={null} linkKind="direct" />
            heard directly
          </li>
          <li>
            <LinkSwatch tier={null} linkKind="neighbor" />
            neighbour
          </li>
        </>
      ) : null}
      <li
        className="minimap-key-tiers"
        title="Signal heard at each half's end, as on the Map"
      >
        {LINK_TIERS.map(({ tier, label }) => (
          <span key={tier} title={label}>
            <LinkSwatch tier={tier} linkKind="traceroute" />
          </span>
        ))}
        signal
      </li>
      {gateways ? (
        <li
          title={`From its ${contacts.packets.toLocaleString()} newest packets${contacts.limited ? "; older ones were not read" : ""}`}
        >
          <span className="contacts-legend-hops hop-tone-1">1</span>
          fewest hops to a gateway
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
  const counts = `${measurements.toLocaleString()} survey ${measurements === 1 ? "measurement" : "measurements"} in view · ${guessed.toLocaleString()} guessed from the relay byte`;
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
    for (const layer of LINK_LAYERS) {
      if (map.getLayer(layer)) map.removeLayer(layer);
    }
    for (const kind of LINK_KINDS) {
      if (map.getSource(linkSourceId(kind)))
        map.removeSource(linkSourceId(kind));
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
): string {
  if (error !== null) return `Position history could not load: ${error}`;
  if (history === null) return "Loading position history…";
  const { items, invalid, limited } = history;
  const count =
    items.length === 0
      ? "No valid position packets of its own."
      : `${items.length.toLocaleString()} ${items.length === 1 ? "position" : "positions"} · click a dot for its packet`;
  const skipped = invalid > 0 ? ` · ${invalid.toLocaleString()} invalid` : "";
  const older = limited ? " · older not drawn" : "";
  return count + skipped + older;
}

function historyBounds(
  items: NodePositionHistory["items"],
  current: Current | null,
): maplibregl.LngLatBounds {
  const bounds = new maplibregl.LngLatBounds();
  for (const item of items) bounds.extend([item.longitude, item.latitude]);
  if (current !== null) bounds.extend([current.longitude, current.latitude]);
  return bounds;
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
    time.textContent = new Date(packet.at).toLocaleString();
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
