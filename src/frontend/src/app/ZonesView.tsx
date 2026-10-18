import {
  Fragment,
  type MouseEvent,
  type RefObject,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  ZONE_WINDOWS,
  type NodeSummary,
  type ZoneWindow,
  type Zone,
  type KeyRelay,
  type NetworkZones,
  type NetworkZonesRequest,
  type ZonePair,
  type ZonePairLink,
  networkZonesUrl,
} from "./api";
import { ZoneCharts } from "./ZoneCharts";
import {
  LATE_HOURS,
  LOW_BATTERY,
  RELAY_COLOR,
  RELAY_MAST,
  RELAY_MAST_TIP,
  WARNING_COLOR,
  type PairWarning,
  type ReachLookup,
  type TextReachFigure,
  drawnLinks,
  formatHours,
  formatShare,
  holdsOnlySolidLink,
  hoursSince,
  isDirect,
  pairKey,
  isOutlined,
  placed,
  relayConcern,
  relayReason,
  sideRelays,
  solidPathLinks,
  textReachLookup,
  type GroupLine,
  type ZoneGroup,
  type ZoneGraph as Graph,
  worseTextShare,
  withoutLoneMembers,
  zoneColors,
  PATTERN_LABELS,
  ZONE_GREY,
  groupCaption,
  lineColor,
  zoneGraph,
  zoneName,
} from "./zones";
import { ZoneGraph } from "./ZoneGraph";
import { type MapFocus, ZoneMap } from "./ZoneMap";
import { useSetting, useViewState } from "./remember";
import { LinkDetails } from "./LinkDetails";
import { type LinkEnd, MapLinkDetails } from "./MapLinkDetails";
import {
  bothWaysObservations,
  LINK_TIERS,
  linkTierColor,
  marginTier,
} from "./linkQuality";
import { type ShareTier, SHARE_COLORS, tierOfShare } from "./mapNodeLayers";
import {
  formatKm,
  formatNodeId,
  formatRole,
  lineOfSightHref,
  mapHref,
  nodeHref,
  nodeName,
  shortLabel,
  tracerouteHopHref,
} from "./nodeDisplay";
import {
  BackButton,
  Badge,
  BothWays,
  DataItem,
  DirectionSnr,
  ErrorPanel,
  Icon,
  IconButton,
  MarginValue,
  NodeLink,
  SortHeading,
  StatusPanel,
  TierDot,
  WideTable,
} from "./ui";
import { openedAt, useApi } from "./useApi";

// The Network network page: zones of well-linked nodes, how strongly they are
// joined, and the relays everything depends on. The backend (zones.py) does
// the graph work; this page lays it out.

const WINDOW_LABELS: Record<ZoneWindow, string> = {
  24: "24 hours",
  72: "3 days",
  168: "7 days",
};
const PER_DAY_CHOICES = [1, 2, 3, 5];
const MARGIN_CHOICES = [0, 2, 4, 6, 10];
const JOIN_CHOICES = [1, 2, 3, 4];
// Rows shown before "Show all".
const PAIR_ROWS = 10;
const RELAY_ROWS = 10;
// Longer node names are cut in the key relays table.
const RELAY_NAME_LENGTH = 20;
const HOUR_MS = 60 * 60 * 1000;
// The warnings about a connection's solid links, which a zone's connections show.
const SOLID_WARNINGS: PairWarning[] = [
  "no solid link",
  "one solid link",
  "one relay",
];

type NodeIndex = Map<number, NodeSummary>;

// Opens and closes the build steps and the legend together, in both views.
const PANEL_OPEN_KEY = "zonesLegendOpen";

type View = "map" | "graph";
const VIEWS: { view: View; icon: "map" | "nodes"; label: string }[] = [
  { view: "map", icon: "map", label: "Map" },
  { view: "graph", icon: "nodes", label: "Graph" },
];
// A moved slider asks for new data once it has rested this long, so dragging
// across several steps reloads the page once.
const SLIDER_SETTLE_MS = 400;

const DEFAULT_REQUEST: NetworkZonesRequest = {
  windowHours: 72,
  channel: null,
  perDay: 2,
  minMarginDb: 4,
  joinLinks: 2,
};

// The last window, channel and graph rules are kept together; a value no
// longer offered puts them all back to the default.
function validRequest(value: unknown): boolean {
  const request = value as NetworkZonesRequest;
  return (
    ZONE_WINDOWS.includes(request.windowHours) &&
    PER_DAY_CHOICES.includes(request.perDay) &&
    MARGIN_CHOICES.includes(request.minMarginDb) &&
    JOIN_CHOICES.includes(request.joinLinks) &&
    (request.channel === null || typeof request.channel === "string")
  );
}

export function ZonesView() {
  const [request, setRequest] = useSetting(
    "zonesRequest",
    DEFAULT_REQUEST,
    validRequest,
  );
  const [selectedZone, setSelectedZone] = useViewState<number | null>(
    "zones.zone",
    null,
  );
  const [selectedPair, setSelectedPair] = useViewState<string | null>(
    "zones.pair",
    null,
  );
  // The selected pair's link, or a link on the selected zone's solid paths,
  // clicked on the map, as `${nodeA}-${nodeB}`.
  const [openLink, setOpenLink] = useViewState<string | null>(
    "zones.link",
    null,
  );
  // The key relay clicked on the map or beside it, whose details open beside
  // the map in place of an open link.
  const [selectedRelay, setSelectedRelay] = useViewState<number | null>(
    "zones.relay",
    null,
  );
  // The build steps and the legend, open or closed together.
  const [panelOpen, setPanelOpen] = useSetting(PANEL_OPEN_KEY, false);
  // One order for a pair's links, beside the map and under its table row.
  const [linkSort, setLinkSort] = useSetting<LinkSort>(
    "zones.linkSort",
    "solid",
    (value) =>
      ["solid", "observations", "margin", "km"].includes(value as string),
  );
  // A group of zones, by its key, or a line between two groups, selected in
  // the graph; a zone or pair chosen from it goes on top and goes back to it.
  const [selectedGroup, setSelectedGroup] = useViewState<number | null>(
    "zones.group",
    null,
  );
  const [selectedLine, setSelectedLine] = useViewState<string | null>(
    "zones.line",
    null,
  );
  const [view, setView] = useSetting<View>(
    "zones.view",
    "map",
    (value) => value === "map" || value === "graph",
  );
  const [focus, setFocus] = useState<MapFocus | null>(null);
  // A visit minutes after the last shows what was loaded then (openedAt).
  const [openedTime] = useState(() => openedAt("zones"));
  const { data, error, loading } = useApi<NetworkZones>(
    networkZonesUrl(request),
    openedTime,
  );
  // The last result stays on screen while a changed control reloads, so the
  // map is not rebuilt.
  const [shown, setShown] = useState<NetworkZones | null>(null);
  if (data !== null && data !== shown) setShown(data);

  const nodes = useMemo<NodeIndex>(
    () => new Map((shown?.nodes ?? []).map((node) => [node.node_id, node])),
    [shown],
  );
  const colors = useMemo(() => zoneColors(shown?.zones ?? []), [shown]);
  const names = useMemo(
    () =>
      new Map(
        (shown?.zones ?? []).map((zone) => [zone.zone_id, zoneName(zone)]),
      ),
    [shown],
  );

  function change(next: Partial<NetworkZonesRequest>) {
    // From the current request: two sliders can settle one after the other
    // before the page renders again.
    setRequest((current) => ({ ...current, ...next }));
    setSelectedZone(null);
    setSelectedPair(null);
    setOpenLink(null);
    setSelectedRelay(null);
    setSelectedGroup(null);
    setSelectedLine(null);
  }

  function selectGroup(key: number | null) {
    setSelectedGroup(key);
    setSelectedLine(null);
    selectZone(null);
  }

  function selectLine(key: string | null) {
    setSelectedLine(key);
    setSelectedGroup(null);
    selectZone(null);
  }

  function selectZone(zoneId: number | null) {
    setSelectedZone(zoneId);
    setSelectedPair(null);
    setOpenLink(null);
  }

  function selectPair(key: string | null) {
    setSelectedPair(key);
    setOpenLink(null);
  }

  // A zone row beside the map or in the graph selects the zone and fits the
  // map to its members, leaving out lone ones as its shaded area does.
  function selectAndShowZone(zoneId: number) {
    selectZone(zoneId);
    const members = shown?.zones.find((item) => item.zone_id === zoneId);
    const points = (members?.members ?? []).flatMap((id) => {
      const point = placed(nodes.get(id));
      return point === null ? [] : [point];
    });
    const area = withoutLoneMembers(points);
    setFocus({ points: area.length > 0 ? area : points });
  }

  // A solid link row under a line's connection opens the pair with that link.
  function selectPairLink(key: string, link: string) {
    selectPair(key);
    openPairLink(link);
  }

  function openPairLink(link: string | null) {
    setOpenLink(link);
    if (link !== null) setSelectedRelay(null);
  }

  function selectRelay(nodeId: number | null) {
    setSelectedRelay(nodeId);
    if (nodeId !== null) setOpenLink(null);
  }

  // A key relay row beside the map selects the relay and centres the map on it.
  function showRelay(nodeId: number) {
    selectRelay(nodeId);
    const point = placed(nodes.get(nodeId));
    if (point !== null) setFocus({ points: [point] });
  }

  const zone = shown?.zones.find((item) => item.zone_id === selectedZone);
  const pair = shown?.zone_pairs.find((item) => pairKey(item) === selectedPair);
  const openedLink = (
    pair?.links ?? (shown === null ? [] : solidPathLinks(shown, selectedZone))
  ).find((link) => linkKey(link) === openLink);
  const relay = shown?.key_relays.find(
    (item) => item.node_id === selectedRelay,
  );
  const reach = useMemo(
    () => textReachLookup(shown ?? { zones: [], text_reach: [] }),
    [shown],
  );
  const graph = useMemo(
    () => zoneGraph(shown ?? { zone_pairs: [], groups: [], group_lines: [] }),
    [shown],
  );
  // Groups and lines are chosen in the graph and shown only beside it; the
  // selection comes back with the graph.
  const group =
    view === "graph"
      ? graph.groups.find((item) => item.key === selectedGroup)
      : undefined;
  const line =
    view === "graph"
      ? graph.lines.find((item) => item.key === selectedLine)
      : undefined;
  // The side panel lists only the key relays that matter most, under a
  // selected zone; the table under the map keeps them all.
  const relays = useMemo(() => sideRelays(shown?.key_relays ?? []), [shown]);
  // A selected zone's connections, most links first, and its key relays.
  const joins = (shown?.zone_pairs ?? [])
    .filter(
      (item) =>
        isDirect(item) &&
        (item.zone_a === zone?.zone_id || item.zone_b === zone?.zone_id),
    )
    .sort((a, b) => b.links.length - a.links.length);
  // Those with a warning first, as they are the ones to go and look at; the
  // stable sort keeps the impact order within each.
  const generatedAt = Date.parse(shown?.generated_at ?? "");
  const zoneRelays = relays
    .filter((relay) => relay.zone_id === zone?.zone_id)
    .sort(
      (a, b) =>
        Number(relayConcern(b, generatedAt) !== null) -
        Number(relayConcern(a, generatedAt) !== null),
    );
  // The side panel's lists: a pair's links, a zone's connections and key relays,
  // or the zones.
  const side = useRef<HTMLElement>(null);
  const { rows: sideRows, grow: sideGrows } = useRowsThatFit(
    side,
    pair !== undefined
      ? pair.links.length
      : zone !== undefined
        ? joins.length + zoneRelays.length
        : (shown?.zones.length ?? 0),
    [
      shown?.generated_at,
      selectedZone,
      selectedPair,
      selectedGroup,
      selectedLine,
      view,
      openLink,
      selectedRelay,
      linkSort,
    ].join(),
  );

  return (
    <section className="zones-page" aria-label="Zones">
      {/* The two views as tabs on the settings panel, whose rules and legend
          change with them, and beside the tabs what both views show. */}
      <div className="zones-head">
        <div className="zones-view-tabs" role="group" aria-label="View">
          {VIEWS.map((item) => (
            <button
              key={item.view}
              type="button"
              aria-pressed={view === item.view}
              aria-label={item.label}
              onClick={() => setView(item.view)}
            >
              <Icon name={item.icon} />
              <strong>{item.label}</strong>
            </button>
          ))}
        </div>
        <div className="zones-controls">
          <label>
            Window
            <select
              value={request.windowHours}
              onChange={(event) =>
                change({
                  windowHours: Number(event.target.value) as ZoneWindow,
                })
              }
            >
              {ZONE_WINDOWS.map((hours) => (
                <option key={hours} value={hours}>
                  {WINDOW_LABELS[hours]}
                </option>
              ))}
            </select>
          </label>
          <label>
            Channel
            <select
              value={request.channel ?? shown?.channel ?? ""}
              onChange={(event) => change({ channel: event.target.value })}
            >
              {shown === null ? <option value="">Busiest</option> : null}
              {shown?.channels.map((item) => (
                <option key={item.channel} value={item.channel}>
                  {item.channel} ({item.links.toLocaleString()} links)
                </option>
              ))}
              {request.channel !== null &&
              !shown?.channels.some(
                (item) => item.channel === request.channel,
              ) ? (
                <option value={request.channel}>{request.channel}</option>
              ) : null}
            </select>
          </label>
        </div>
      </div>
      {/* The settings over the map, and beside the map the zones, as tall as
          it; narrow screens stack them (styles.css). */}
      <div className="zones-layout">
        {shown === null ? null : (
          <section
            className="zones-panel zones-settings zones-tabbed"
            aria-label="Settings"
          >
            <BuildSteps
              network={shown}
              graph={graph}
              request={request}
              view={view}
              open={panelOpen}
              onToggle={setPanelOpen}
              onChange={change}
            />
            {view === "graph" ? (
              <GraphLegend
                network={shown}
                open={panelOpen}
                onToggle={setPanelOpen}
              />
            ) : (
              <MapLegend
                network={shown}
                open={panelOpen}
                onToggle={setPanelOpen}
              />
            )}
            {loading ? (
              <p className="zones-note" role="status">
                Working out the network…
              </p>
            ) : null}
          </section>
        )}
        <div className="zones-column zones-main-column">
          {error !== null && shown !== null ? (
            <ErrorPanel message={error} />
          ) : null}
          {shown === null ? (
            error !== null ? (
              <StatusPanel tone="error">{error}</StatusPanel>
            ) : (
              <StatusPanel>Working out the network…</StatusPanel>
            )
          ) : view === "graph" ? (
            <section
              className="zones-panel zones-graph-panel"
              aria-label="Graph"
            >
              <ZoneGraph
                graph={graph}
                colors={colors}
                names={names}
                selectedGroup={group !== undefined ? selectedGroup : null}
                selectedLine={line !== undefined ? selectedLine : null}
                selectedZone={selectedZone}
                onSelectGroup={selectGroup}
                onSelectLine={selectLine}
                onSelectZone={selectAndShowZone}
              />
            </section>
          ) : (
            <ZoneMap
              network={shown}
              nodes={nodes}
              colors={colors}
              names={names}
              selectedZone={selectedZone}
              onSelectZone={selectZone}
              selectedPair={selectedPair}
              onSelectPair={selectPair}
              openLink={openedLink !== undefined ? openLink : null}
              onOpenLink={openPairLink}
              selectedRelay={relay !== undefined ? selectedRelay : null}
              onSelectRelay={selectRelay}
              focus={focus}
            />
          )}
        </div>
        {shown !== null ? (
          <div className="zones-column zones-side-column">
            {/* A group's or line's details are a few rows and grow the
                panel rather than being cut, as do details too tall to fit
                beside the map with none of their list's rows. */}
            <aside
              ref={side}
              className={`zones-panel zones-side${
                sideGrows ||
                (pair === undefined &&
                  zone === undefined &&
                  (line !== undefined || group !== undefined))
                  ? " is-whole"
                  : ""
              }`}
              aria-label="Zones"
            >
              {relay !== undefined ? (
                <RelayCard
                  relay={relay}
                  network={shown}
                  nodes={nodes}
                  colors={colors}
                  names={names}
                  onSelectZone={selectZone}
                  onClose={() => selectRelay(null)}
                />
              ) : null}
              {openedLink !== undefined ? (
                <section
                  className="selected-node zones-open-link"
                  aria-live="polite"
                >
                  <MapLinkDetails
                    linkKind="traceroute"
                    link={{ ...openedLink, channel: shown.channel ?? "" }}
                    nodeA={linkEnd(openedLink.node_a, nodes)}
                    nodeB={linkEnd(openedLink.node_b, nodes)}
                    km={openedLink.km}
                    since={new Date(
                      Date.parse(shown.generated_at) -
                        shown.window_hours * HOUR_MS,
                    ).toISOString()}
                    onClose={() => setOpenLink(null)}
                    linkSection={false}
                  />
                </section>
              ) : null}
              {/* A pair goes before the zone it was chosen from; its × goes
                  back to that zone. */}
              {pair !== undefined ? (
                <PairSide
                  pair={pair}
                  nodes={nodes}
                  colors={colors}
                  names={names}
                  reach={reach}
                  sort={linkSort}
                  onSort={setLinkSort}
                  rows={sideRows}
                  openLink={openedLink !== undefined ? openLink : null}
                  onOpenLink={openPairLink}
                  onSelectZone={selectZone}
                  backTo={
                    zone !== undefined
                      ? `${zone.zone_id}. ${names.get(zone.zone_id) ?? zone.name}`
                      : line !== undefined
                        ? [line.a, line.b]
                            .flatMap(
                              (key) =>
                                graph.groups.find((item) => item.key === key) ??
                                [],
                            )
                            .map((item) => groupCaption(item, names))
                            .join(" ↔ ")
                        : group !== undefined
                          ? groupCaption(group, names)
                          : undefined
                  }
                  onBack={() => selectPair(null)}
                />
              ) : zone !== undefined ? (
                <ZoneDetails
                  zone={zone}
                  joins={joins}
                  relays={zoneRelays}
                  network={shown}
                  nodes={nodes}
                  colors={colors}
                  names={names}
                  rows={sideRows}
                  onSelectPair={selectPair}
                  selectedRelay={selectedRelay}
                  onShowNode={showRelay}
                  onBack={() => selectZone(null)}
                />
              ) : line !== undefined ? (
                <LineDetails
                  line={line}
                  graph={graph}
                  nodes={nodes}
                  colors={colors}
                  names={names}
                  reach={reach}
                  onSelectGroup={selectGroup}
                  onSelectPair={selectPair}
                  onSelectLink={selectPairLink}
                  onBack={() => selectLine(null)}
                />
              ) : group !== undefined ? (
                <GroupDetails
                  group={group}
                  network={shown}
                  colors={colors}
                  names={names}
                  reach={reach}
                  onSelectZone={selectAndShowZone}
                  onSelectPair={selectPair}
                  onBack={() => selectGroup(null)}
                />
              ) : (
                <SideLists
                  network={shown}
                  nodes={nodes}
                  colors={colors}
                  names={names}
                  relays={relays}
                  rows={sideRows}
                  onSelectZone={selectAndShowZone}
                />
              )}
            </aside>
          </div>
        ) : null}
      </div>

      {shown !== null ? (
        <>
          <BetweenZones
            network={shown}
            nodes={nodes}
            colors={colors}
            names={names}
            selectedZone={selectedZone}
            selectedPair={selectedPair}
            onSelectZone={selectZone}
            onSelectPair={selectPair}
            linkSort={linkSort}
            onLinkSort={setLinkSort}
          />
          <KeyRelays
            network={shown}
            nodes={nodes}
            colors={colors}
            names={names}
            selectedZone={selectedZone}
            onSelectZone={selectZone}
            selectedRelay={selectedRelay}
          />
          <ZoneCharts network={shown} />
        </>
      ) : null}
    </section>
  );
}

/**
 * A key relay's mast as the zone map draws it (relayImage in ZoneMap.tsx),
 * without the white edge and halo that set it apart from the basemap: on a
 * dark page they would look like a second outline.
 */
function RelayMark({ filled }: { filled: boolean }) {
  const color = filled ? "#fff" : RELAY_COLOR;
  return (
    <svg
      className="zones-relay-mark"
      width="18"
      height="18"
      viewBox="-9 -9 18 18"
      aria-hidden="true"
    >
      {filled ? <circle r="9" fill={RELAY_COLOR} /> : null}
      <g
        transform={`scale(${filled ? 0.56 : 0.7})`}
        fill={color}
        stroke={color}
        strokeWidth={filled ? 2.2 : 2.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={RELAY_MAST} fill="none" />
        <path d={RELAY_MAST_TIP} />
      </g>
    </svg>
  );
}

/** A key relay's warning as the zone map draws it (warningImage in ZoneMap.tsx). */
function WarningMark() {
  return (
    <svg
      className="zones-relay-mark"
      width="14"
      height="14"
      viewBox="0 0 14 14"
      aria-hidden="true"
    >
      <path
        d="M7 1.5 L13 12.5 H1 Z"
        fill={WARNING_COLOR}
        stroke={WARNING_COLOR}
        strokeLinejoin="round"
      />
      <path
        d="M7 5.5 V8.8 M7 10.6 V10.7"
        stroke="#1f1a10"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

/**
 * A zone line as the map draws it (ZoneMap.tsx), and the graph a line between
 * groups: wide, thin, or thin and dashed.
 */
function legendLine(stroke: number, dashes: string | undefined, width = 28) {
  return (
    <svg width={width} height="10" aria-hidden="true">
      <line
        x1="1"
        y1="5"
        x2={width - 1}
        y2="5"
        stroke="currentColor"
        strokeWidth={stroke}
        strokeDasharray={dashes}
      />
    </svg>
  );
}

/** The text reach each colour stands for, as tierOfShare splits it. */
const SHARE_TIER_LABELS: { tier: ShareTier; label: string }[] = [
  { tier: "high", label: "80%+" },
  { tier: "medium", label: "50–79%" },
  { tier: "low", label: "21–49%" },
  { tier: "poor", label: "20% or less" },
];

/**
 * The rules the graph is built from, as numbered steps, each with its
 * settings and what it gave: links, the solid ones among them, and the groups
 * solid links make. The map uses only the first two, and shows only those.
 * Opens and closes with the legend; closed, only the sliders show.
 */
function BuildSteps({
  network,
  graph,
  request,
  view,
  open,
  onToggle,
  onChange,
}: {
  network: NetworkZones;
  graph: Graph;
  request: NetworkZonesRequest;
  view: View;
  open: boolean;
  onToggle: (open: boolean) => void;
  onChange: (next: Partial<NetworkZonesRequest>) => void;
}) {
  const changed =
    request.perDay !== DEFAULT_REQUEST.perDay ||
    request.minMarginDb !== DEFAULT_REQUEST.minMarginDb ||
    (view === "graph" && request.joinLinks !== DEFAULT_REQUEST.joinLinks);
  const perDay = (
    <StepSlider
      label="Observations a day, each direction"
      choices={PER_DAY_CHOICES}
      value={request.perDay}
      format={(value) => `${value}× a day`}
      onChange={(perDay) => onChange({ perDay })}
    />
  );
  const margin = (
    <StepSlider
      label="SNR margin above the channel floor"
      choices={MARGIN_CHOICES}
      value={request.minMarginDb}
      format={(value) => `${value} dB`}
      onChange={(minMarginDb) => onChange({ minMarginDb })}
    />
  );
  const join = (
    <StepSlider
      label="Solid links that join two zones"
      choices={JOIN_CHOICES}
      value={request.joinLinks}
      format={(value) => plural(value, "solid link")}
      onChange={(joinLinks) => onChange({ joinLinks })}
    />
  );
  const reset = changed ? (
    <button
      type="button"
      className="text-button"
      onClick={() =>
        onChange({
          perDay: DEFAULT_REQUEST.perDay,
          minMarginDb: DEFAULT_REQUEST.minMarginDb,
          joinLinks: DEFAULT_REQUEST.joinLinks,
        })
      }
    >
      Reset
    </button>
  ) : null;
  return (
    <section
      className={`zones-steps${open ? "" : " is-closed"}`}
      aria-label="How the graph is built"
    >
      <details
        open={open}
        onToggle={(event) => onToggle(event.currentTarget.open)}
      >
        <summary>
          <h3>
            {view === "graph"
              ? "How this graph is built"
              : "Which links are solid"}
          </h3>
        </summary>
        {/* Rendered only open, so each slider is on the page once. */}
        {open ? (
          <>
            <div className="zones-steps-heading">
              <span className="muted">
                {view === "graph"
                  ? "These settings decide the boxes and lines; changing one redraws the graph."
                  : "Solid links set the lines' widths and the key relays."}
              </span>
              {reset}
            </div>
            <ol>
              <li>
                <strong>Links</strong>
                <p>
                  Every traceroute hop observed at least twice in any direction.
                </p>
              </li>
              <li>
                <strong>Solid links</strong>
                <p>
                  Heard <em>both</em> ways at least {perDay}, {margin} above the
                  channel floor.
                </p>
                <p className="zones-step-result">
                  {network.solid_link_count.toLocaleString()} of{" "}
                  {plural(network.link_count, "link")} are solid
                </p>
              </li>
              {view === "graph" ? (
                <li>
                  <strong>Groups</strong>
                  <p>
                    Two boxes join when at least {join} link them, counted
                    across all their zones.
                  </p>
                  <p className="zones-step-result">
                    {plural(network.zones.length, "zone")} in{" "}
                    {plural(graph.groups.length, "group")}
                  </p>
                </li>
              ) : null}
            </ol>
          </>
        ) : null}
      </details>
      {open ? null : (
        <div className="zones-steps-sliders">
          <label>Observations {perDay}</label>
          <label>SNR margin {margin}</label>
          {view === "graph" ? <label>Joining links {join}</label> : null}
          {reset}
        </div>
      )}
    </section>
  );
}

/**
 * A slider over a few set values that shows the value it is on. It asks for
 * the new value once the slider has rested (SLIDER_SETTLE_MS), and follows a
 * value changed elsewhere, such as by Reset.
 */
function StepSlider({
  label,
  choices,
  value,
  format,
  onChange,
}: {
  label: string;
  choices: number[];
  value: number;
  format: (value: number) => string;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [settled, setSettled] = useState(value);
  if (value !== settled) {
    setSettled(value);
    setDraft(value);
  }
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  function move(next: number) {
    setDraft(next);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => onChange(next), SLIDER_SETTLE_MS);
  }
  return (
    <span className="zones-step-slider">
      <input
        type="range"
        min={0}
        max={choices.length - 1}
        step={1}
        value={Math.max(0, choices.indexOf(draft))}
        aria-label={label}
        aria-valuetext={format(draft)}
        onChange={(event) => move(choices[Number(event.target.value)])}
      />
      <output>{format(draft)}</output>
    </span>
  );
}

/**
 * Explains the zone graph's marks, in the map legend's place: closed, a
 * one-line key; open, what boxes, lines and colours mean.
 */
function GraphLegend({
  network,
  open,
  onToggle,
}: {
  network: NetworkZones;
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  const swatch = (color: string) => (
    <span className="zones-legend-swatch" style={{ background: color }} />
  );
  return (
    <details
      className="zones-legend"
      open={open}
      onToggle={(event) => onToggle(event.currentTarget.open)}
    >
      <summary aria-label="Legend">
        <span className="zones-legend-key">
          <span className="zones-legend-heading">
            Line colour: best text reach
          </span>
          {SHARE_TIER_LABELS.map(({ tier, label }) => (
            <span key={tier} title={`Text reach ${label}`}>
              {swatch(SHARE_COLORS[tier])} {label}
            </span>
          ))}
          <span
            title={`No direction with ${network.rules.few_texts} or more texts`}
          >
            {swatch(ZONE_GREY)} too few texts
          </span>
          <span title="2 or more solid links">
            {legendLine(6, undefined, 18)} {PATTERN_LABELS.solid}
          </span>
          <span title="One solid link or one relay">
            {legendLine(2, undefined, 18)} {PATTERN_LABELS["one solid link"]}
          </span>
          <span title="No solid link">
            {legendLine(2, "5 3", 18)} {PATTERN_LABELS.weak}
          </span>
        </span>
      </summary>
      <p>
        <strong>Boxes</strong> are groups. Under each box, the lowest and
        highest text reach between two of its zones.
      </p>
      <p>
        <strong>Line labels</strong>: lowest and highest text reach between the
        groups.
      </p>
      <ul
        className="zones-legend-items"
        title="Solid links between the groups. One relay: all through one node."
      >
        <li>{legendLine(6, undefined)} 2 or more solid links</li>
        <li>{legendLine(2, undefined)} one solid link or one relay</li>
        <li>{legendLine(2, "5 3")} no solid link</li>
      </ul>
      {network.text_reach_truncated ? (
        <p>Text reach uses only the newest 10,000 texts on the channel.</p>
      ) : null}
    </details>
  );
}

/**
 * Explains the zone map's marks; colours come from the map's own constants.
 * Closed, the summary is a one-line key; open, it explains each mark.
 */
function MapLegend({
  network,
  open,
  onToggle,
}: {
  network: NetworkZones;
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  return (
    <details
      className="zones-legend"
      open={open}
      onToggle={(event) => onToggle(event.currentTarget.open)}
    >
      <summary aria-label="Legend">
        <span className="zones-legend-key">
          {LINK_TIERS.filter((entry) => entry.tier !== "unknown").map(
            (entry) => (
              <span key={entry.tier} title={entry.description}>
                <span
                  className="zones-legend-swatch"
                  style={{ background: entry.color }}
                />{" "}
                {entry.label.toLowerCase()}
              </span>
            ),
          )}
          <span title="2 or more solid links">
            {legendLine(6, undefined, 18)} 2+ solid links
          </span>
          <span title="One solid link or one relay">
            {legendLine(2, undefined, 18)} one solid link/relay
          </span>
          <span title="No solid link">
            {legendLine(2, "5 3", 18)} no solid link
          </span>
          <span title="Carries the only solid link between two zones">
            <RelayMark filled /> only solid link
          </span>
          <span title="Other nodes depend on it">
            <RelayMark filled={false} /> key relay
          </span>
          <span
            title={`A key relay not heard for ${LATE_HOURS} hours or on a low battery`}
          >
            <WarningMark /> warning
          </span>
          <span title="Estimated position: the node reports none">
            <span className="zones-legend-hollow" /> estimated position
          </span>
        </span>
      </summary>
      <p>
        Click on a line to view all the direct links between two zones. The
        colour is the strongest link&apos;s signal margin:{" "}
        {LINK_TIERS.map((entry, index) => (
          <Fragment key={entry.tier}>
            {index > 0 ? ", " : ""}
            <span
              className="zones-legend-swatch"
              style={{ background: entry.color }}
            />{" "}
            {entry.label.toLowerCase()}
          </Fragment>
        ))}
        .
      </p>
      <ul
        className="zones-legend-items"
        title="Solid links between the zones. One relay: all through one node."
      >
        <li>{legendLine(6, undefined)} 2 or more solid links</li>
        <li>{legendLine(2, undefined)} one solid link or one relay</li>
        <li>{legendLine(2, "5 3")} no solid link</li>
      </ul>
      <p>
        <RelayMark filled /> <strong>Red disc</strong>: the only solid link
        between two zones runs through this relay.
      </p>
      <p>
        <RelayMark filled={false} /> <strong>Bare mast</strong>: a relay other
        nodes depend on. Bigger means more nodes.
      </p>
      <p>
        <WarningMark /> <strong>Warning triangle</strong>: a key relay that has
        gone quiet or is low on battery. Hover for why.
      </p>
      <p>
        <span className="zones-legend-hollow" /> <strong>Hollow dots</strong>{" "}
        are nodes that report no position, placed among the neighbours that hear
        them.
      </p>
      {network.truncated ? (
        <p>
          This channel has more links than the page reads, so the oldest are
          missing.
        </p>
      ) : null}
      {network.text_reach_truncated ? (
        <p>Text reach uses only the newest 10,000 texts on the channel.</p>
      ) : null}
    </details>
  );
}

// ------------------------------------------------------------ Beside the map

// The fewest rows of a list beside the map that the panel keeps, growing past
// the map's bottom when even these do not fit.
const MIN_FIT_ROWS = 3;

/**
 * How many of the side panel list's `total` rows fit without the panel
 * scrolling. Beside the map the panel is as tall as the map's column
 * (styles.css), and the rows marked data-fit-row are dropped from the end
 * while it overflows, down to MIN_FIT_ROWS; stacked on a narrow screen it
 * grows and shows them all. `grow` says the panel overflows even then, such
 * as with a link's or relay's details open, and should grow rather than be
 * cut. A change of `content`, the row count or the panel's size starts again
 * from every row.
 */
function useRowsThatFit(
  panel: RefObject<HTMLElement | null>,
  total: number,
  content: string,
): { rows: number; grow: boolean } {
  const [size, setSize] = useState("");
  const key = `${content}|${total}|${size}`;
  const [fit, setFit] = useState({ key, rows: total, grow: false });
  if (fit.key !== key) setFit({ key, rows: total, grow: false });
  const current = fit.key === key ? fit : { rows: total, grow: false };
  const rows = current.rows;

  useEffect(() => {
    const element = panel.current;
    if (element === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() =>
      setSize(`${element.clientWidth}x${element.clientHeight}`),
    );
    observer.observe(element);
    return () => observer.disconnect();
    // The panel appears once the zones load, which changes `content`.
  }, [panel, content]);

  useLayoutEffect(() => {
    const element = panel.current;
    if (element === null) return;
    if (getComputedStyle(element).position !== "absolute") return;
    const over = element.scrollHeight - element.clientHeight;
    if (over <= 0) return;
    // Measuring before paint and cutting is what this effect is for; each
    // cut re-renders once, and it stops when the panel fits or grows (which
    // takes it out of absolute position).
    const fewest = Math.min(total, MIN_FIT_ROWS);
    if (rows <= fewest) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setFit({ key, rows, grow: true });
      return;
    }
    const marked = element.querySelectorAll<HTMLElement>("[data-fit-row]");
    let freed = 0;
    let cut = 0;
    while (cut < marked.length && freed < over) {
      freed += marked[marked.length - 1 - cut].offsetHeight;
      cut += 1;
    }
    setFit({
      key,
      rows: Math.max(fewest, rows - Math.max(cut, 1)),
      grow: false,
    });
  }, [panel, rows, total, key]);
  return current;
}

/**
 * At the right of a side list's heading: how many of its rows fit, when not
 * all do, and with `target` (an element id) a button that scrolls to the
 * full table under the map.
 */
function MoreInTable({
  shown,
  total,
  target,
  title,
}: {
  shown: number;
  total: number;
  target?: string;
  title?: string;
}) {
  const cut = shown < total;
  if (!cut && target === undefined) return null;
  return (
    <span className="zones-more" title={title}>
      {cut ? `${shown} of ${total.toLocaleString()}` : null}
      {cut && target !== undefined ? " · " : null}
      {target !== undefined ? (
        <button
          type="button"
          className="text-button"
          onClick={() =>
            document
              .getElementById(target)
              ?.scrollIntoView({ behavior: "smooth", block: "center" })
          }
        >
          In the table ↓
        </button>
      ) : null}
    </span>
  );
}

/**
 * Beside the map while nothing is selected: the network in one line, then
 * the zones.
 */
function SideLists({
  network,
  nodes,
  colors,
  names,
  relays,
  rows,
  onSelectZone,
}: {
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  // The key relays the side panel lists (sideRelays).
  relays: KeyRelay[];
  rows: number;
  onSelectZone: (zoneId: number) => void;
}) {
  if (network.zones.length === 0) {
    return (
      <p className="zones-note">
        No zones: this channel has too few traceroute links in the window.
      </p>
    );
  }
  const members = network.zones.reduce(
    (sum, zone) => sum + zone.members.length,
    0,
  );
  const total = network.zones.length;
  const islands = new Set(network.zones.map((zone) => zone.island)).size;
  return (
    <>
      <p className="zones-summary">
        {plural(network.zones.length, "zone")} in {plural(islands, "island")} ·{" "}
        {plural(members, "node")}
      </p>
      <div className="zones-side-heading">
        <MoreInTable
          shown={Math.min(rows, total)}
          total={total}
          title="Select the others on the map"
        />
      </div>
      <ZoneTable
        network={network}
        relays={relays}
        nodes={nodes}
        colors={colors}
        names={names}
        rows={rows}
        onSelectZone={onSelectZone}
      />
    </>
  );
}

/** The zones, a line each; a row selects its zone. */
function ZoneTable({
  network,
  relays,
  nodes,
  colors,
  names,
  rows,
  onSelectZone,
}: {
  network: NetworkZones;
  relays: KeyRelay[];
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  rows: number;
  onSelectZone: (zoneId: number) => void;
}) {
  const now = Date.parse(network.generated_at);
  return (
    <table className="zones-table zones-side-table">
      <thead>
        <tr>
          <th
            scope="col"
            title="Groups of nodes that mostly hear each other, named after the nearest big town. Select one for details."
          >
            Zone
          </th>
          <th
            scope="col"
            className="number"
            title="Members; hover a number for those with a position"
          >
            Nodes
          </th>
          <th
            scope="col"
            className="number"
            title="Heard links between the zone's members"
          >
            Links
          </th>
          <th
            scope="col"
            className="number"
            title="Zones it has heard links with directly, drawn as lines on the map"
          >
            Connections
          </th>
          <th
            scope="col"
            className="number"
            title={`The zone's key relays not heard for ${LATE_HOURS} hours or on a low battery`}
          >
            Warnings
          </th>
        </tr>
      </thead>
      <tbody>
        {network.zones.slice(0, rows).map((zone) => {
          const touches = (pair: ZonePair) =>
            pair.zone_a === zone.zone_id || pair.zone_b === zone.zone_id;
          const pairs = network.zone_pairs.filter(
            (pair) => isDirect(pair) && touches(pair),
          );
          const concerns = relays.filter(
            (relay) =>
              relay.zone_id === zone.zone_id &&
              relayConcern(relay, now) !== null,
          ).length;
          const placed = zone.members.filter(
            (id) => nodes.get(id)?.latitude != null,
          ).length;
          return (
            <tr
              key={zone.zone_id}
              data-fit-row
              className="zones-pair-row"
              onClick={(event) => {
                if ((event.target as Element).closest("button")) return;
                onSelectZone(zone.zone_id);
              }}
            >
              <td>
                <ZoneChip
                  zoneId={zone.zone_id}
                  colors={colors}
                  names={names}
                  title={[
                    zone.region === null
                      ? null
                      : `${names.get(zone.zone_id)}, ${zone.region}`,
                    `Hub: ${zone.name}`,
                  ]
                    .filter((line) => line !== null)
                    .join("\n")}
                  onSelect={onSelectZone}
                />
              </td>
              <td className="number" title={`${placed} with a position`}>
                {zone.members.length}
              </td>
              <td className="number">{zone.internal_links}</td>
              <td className="number">{pairs.length}</td>
              <td className="number">
                {concerns > 0 ? (
                  <>
                    <WarningMark />
                    {concerns}
                  </>
                ) : null}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * Weak connections, with their worst warning and texts each way; a row selects
 * the pair. With onSelectLink, each pair's solid links follow it, strongest
 * first, with their SNR each way, and a link's row opens it.
 */
function JoinList({
  pairs,
  nodes,
  colors,
  names,
  reach,
  onSelectPair,
  onSelectLink,
}: {
  pairs: ZonePair[];
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  reach: ReachLookup;
  onSelectPair: (pair: string) => void;
  onSelectLink?: (pair: string, link: string) => void;
}) {
  const items = pairs.flatMap((pair) => [
    { pair, link: null },
    ...(onSelectLink === undefined ? [] : pairSolidLinks(pair)).map((link) => ({
      pair,
      link,
    })),
  ]);
  return (
    <ul className="zones-pick-list">
      {items.map(({ pair, link }) => {
        if (link !== null) {
          return (
            <li key={`${pairKey(pair)} ${linkKey(link)}`}>
              <button
                type="button"
                title="Solid link, with its average SNR each way: select it to see both directions"
                onClick={() => onSelectLink?.(pairKey(pair), linkKey(link))}
              >
                <span className="zones-pick-name zones-pick-sub">
                  <span className="zones-pick-text">
                    {directionEnd(link.node_a, nodes)}–
                    {directionEnd(link.node_b, nodes)}
                  </span>
                </span>
                <span className="zones-value">
                  <LinkSnr link={link} nodes={nodes} />
                </span>
              </button>
            </li>
          );
        }
        const warnings = pair.warnings;
        return (
          <li key={pairKey(pair)}>
            <button
              type="button"
              title={[
                ...warnings.map(
                  (warning) => warningTitle(warning, pair, nodes) ?? warning,
                ),
                `Alternative path: ${wayRoundText(pair, names)}`,
              ].join("\n")}
              onClick={() => onSelectPair(pairKey(pair))}
            >
              <span className="zones-pick-name">
                <ZoneNumber zoneId={pair.zone_a} colors={colors} />
                <ZoneNumber zoneId={pair.zone_b} colors={colors} />
                <span className="zones-pick-text">
                  {names.get(pair.zone_a)}–{names.get(pair.zone_b)}
                </span>
              </span>
              <span className="zones-value">
                <span className="zones-late">{warnings[0]}</span>{" "}
                <BothWays
                  left={
                    <TextReach
                      from={pair.zone_a}
                      to={pair.zone_b}
                      figure={reach(pair.zone_a, pair.zone_b)}
                    />
                  }
                  right={
                    <TextReach
                      from={pair.zone_b}
                      to={pair.zone_a}
                      figure={reach(pair.zone_b, pair.zone_a)}
                    />
                  }
                />
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** A pair's solid links, the strongest first. */
function pairSolidLinks(pair: ZonePair): ZonePairLink[] {
  return pair.links
    .filter((link) => link.solid)
    .sort((a, b) => b.weaker_margin_db - a.weaker_margin_db);
}

/**
 * A group line's pattern and texts, as a list row's figures; with count, the
 * texts behind the figure too.
 */
function LineFigures({
  line,
  count = false,
}: {
  line: GroupLine;
  count?: boolean;
}) {
  return (
    <span className="zones-value">
      <span className={line.pattern === "solid" ? undefined : "zones-late"}>
        {line.solidLinks === 0
          ? "no solid link"
          : plural(line.solidLinks, "solid link")}
      </span>
      {line.singleLink ? " · only solid route" : ""} ·{" "}
      <span className="zones-margin">
        <TierDot color={lineColor(line)} />
        {line.texts === null ? "no texts" : formatShare(line.texts)}
      </span>
      {count && line.textCount !== null ? (
        <span className="muted"> · {plural(line.textCount, "text")}</span>
      ) : null}
    </span>
  );
}

/**
 * Zone pairs of a group, with their texts each way and, with hops, the radio
 * hops between their hubs, else how many texts each share counts; a row
 * selects the pair.
 */
function GroupPairList({
  pairs,
  hops,
  colors,
  names,
  reach,
  onSelectPair,
}: {
  pairs: ZonePair[];
  hops: boolean;
  colors: Map<number, string>;
  names: Map<number, string>;
  reach: ReachLookup;
  onSelectPair: (pair: string) => void;
}) {
  return (
    <ul className="zones-pick-list">
      {pairs.map((pair) => (
        <li key={pairKey(pair)}>
          <button type="button" onClick={() => onSelectPair(pairKey(pair))}>
            <span className="zones-pick-name">
              <ZoneNumber zoneId={pair.zone_a} colors={colors} />
              <ZoneNumber zoneId={pair.zone_b} colors={colors} />
              <span className="zones-pick-text">
                {names.get(pair.zone_a)}–{names.get(pair.zone_b)}
              </span>
            </span>
            <span className="zones-value">
              {hops ? (
                <>
                  <span title="The fewest radio hops between the two zone hubs">
                    {pair.hub_hops === null
                      ? "—"
                      : plural(pair.hub_hops, "hop")}
                  </span>{" "}
                </>
              ) : null}
              <BothWays
                left={
                  <TextReach
                    from={pair.zone_a}
                    to={pair.zone_b}
                    figure={reach(pair.zone_a, pair.zone_b)}
                    count={!hops}
                  />
                }
                right={
                  <TextReach
                    from={pair.zone_b}
                    to={pair.zone_a}
                    figure={reach(pair.zone_b, pair.zone_a)}
                    count={!hops}
                  />
                }
              />
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/**
 * A group of zones selected in the graph: its zones, its farthest-apart zone
 * pairs and the pairs that get the fewest texts across. Its lines to other groups are the ones
 * the graph lights up. The panel grows to show it all (styles.css).
 */
function GroupDetails({
  group,
  network,
  colors,
  names,
  reach,
  onSelectZone,
  onSelectPair,
  onBack,
}: {
  group: ZoneGroup;
  network: NetworkZones;
  colors: Map<number, string>;
  names: Map<number, string>;
  reach: ReachLookup;
  onSelectZone: (zoneId: number) => void;
  onSelectPair: (pair: string) => void;
  onBack: () => void;
}) {
  const zones = new Map(network.zones.map((zone) => [zone.zone_id, zone]));
  const [low, high] = group.textsInside ?? [null, null];
  return (
    <>
      <BackButton label="All zones" onClick={onBack} />
      <div className="zones-zone-title">
        <h3>{groupCaption(group, names)}</h3>
      </div>
      <dl className="zones-facts">
        <DataItem
          label="Group"
          value={`${plural(group.zones.length, "zone")} · ${plural(group.nodes, "node")}`}
        />
        {group.zones.length > 1 ? (
          <DataItem
            label="Text reach"
            value={
              low === null || high === null
                ? "too few texts"
                : `${formatShare(low)}–${formatShare(high)} between its zones`
            }
          />
        ) : null}
      </dl>
      <h4>Zones ({group.zones.length})</h4>
      <ul className="zones-group-zones">
        {group.zones.map((zoneId) => (
          <li key={zoneId}>
            <button
              type="button"
              className="zones-zone-chip zones-relay-zone"
              title={`${plural(zones.get(zoneId)?.members.length ?? 0, "node")}. Select zone ${zoneId}`}
              onClick={() => onSelectZone(zoneId)}
            >
              <ZoneNumber zoneId={zoneId} colors={colors} />
              {names.get(zoneId)}
            </button>
          </li>
        ))}
      </ul>
      {group.farthest.length > 0 ? (
        <>
          <h4 title="Zone pairs with the most relays between them">
            Farthest apart
          </h4>
          <GroupPairList
            pairs={group.farthest}
            hops
            colors={colors}
            names={names}
            reach={reach}
            onSelectPair={onSelectPair}
          />
        </>
      ) : null}
      {group.fewestTexts.length > 0 ? (
        <>
          <h4 title="Zone pairs where the fewest texts get across">
            Fewest texts get across
          </h4>
          <GroupPairList
            pairs={group.fewestTexts}
            hops={false}
            colors={colors}
            names={names}
            reach={reach}
            onSelectPair={onSelectPair}
          />
        </>
      ) : null}
    </>
  );
}

/**
 * A line between two groups selected in the graph: the weak connections it stands
 * for, strongest first, each followed by its solid links. The panel grows to
 * show them all (styles.css).
 */
function LineDetails({
  line,
  graph,
  nodes,
  colors,
  names,
  reach,
  onSelectGroup,
  onSelectPair,
  onSelectLink,
  onBack,
}: {
  line: GroupLine;
  graph: Graph;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  reach: ReachLookup;
  onSelectGroup: (key: number) => void;
  onSelectPair: (pair: string) => void;
  onSelectLink: (pair: string, link: string) => void;
  onBack: () => void;
}) {
  const ends = [line.a, line.b].flatMap(
    (key) => graph.groups.find((group) => group.key === key) ?? [],
  );
  return (
    <>
      <BackButton label="All zones" onClick={onBack} />
      <div className="zones-zone-title">
        <h3>{ends.map((group) => groupCaption(group, names)).join(" ↔ ")}</h3>
      </div>
      <p className="zones-note">
        <LineFigures line={line} count />
      </p>
      <p className="zones-note">
        {line.solidLinks === 0
          ? "No solid link joins these groups."
          : `${plural(line.solidLinks, "solid link")} ${line.solidLinks === 1 ? "joins" : "join"} these groups.`}{" "}
        {line.singleLink
          ? "One is the only solid route to part of the network. "
          : ""}
        Texts: best pair, weaker direction.
      </p>
      <div className="zones-tabs" role="group" aria-label="Groups">
        {ends.map((group) => (
          <button
            key={group.key}
            type="button"
            onClick={() => onSelectGroup(group.key)}
          >
            {groupCaption(group, names)}
          </button>
        ))}
      </div>
      <h4>Connections ({line.pairs.length})</h4>
      <JoinList
        pairs={line.pairs}
        nodes={nodes}
        colors={colors}
        names={names}
        reach={reach}
        onSelectPair={onSelectPair}
        onSelectLink={onSelectLink}
      />
    </>
  );
}

/**
 * A key relay clicked on the map or in a list beside it: what it matters for,
 * the nodes that depend on it, and how it is doing, with its page, the Map
 * page and its row in the table.
 */
function RelayCard({
  relay,
  network,
  nodes,
  colors,
  names,
  onSelectZone,
  onClose,
}: {
  relay: KeyRelay;
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  onSelectZone: (zoneId: number) => void;
  onClose: () => void;
}) {
  const nodeId = relay.node_id;
  const node = nodes.get(nodeId);
  const now = Date.parse(network.generated_at);
  const concern = relayConcern(relay, now);
  const hours = hoursSince(relay.last_seen_at, now);
  const nodeCount = (count: number) => plural(count, "node");
  return (
    <section className="selected-node zones-open-link" aria-live="polite">
      <p className="section-label">Selected key relay</p>
      <div className="selected-node-heading">
        <div>
          <h3>
            {holdsOnlySolidLink(relay) ? (
              <RelayMark filled />
            ) : isOutlined(relay, network.rules) ? (
              <RelayMark filled={false} />
            ) : null}{" "}
            <a className="text-button" href={nodeHref(nodeId)}>
              {nodeName(node) ?? formatNodeId(nodeId)}
            </a>
          </h3>
          <span className="mono muted">
            {formatNodeId(nodeId)} · {formatRole(node?.role ?? null)}
          </span>
        </div>
        <IconButton
          icon="close"
          label="Clear selected relay"
          onClick={onClose}
        />
      </div>
      <p>
        {concern !== null ? <span className="zones-late">⚠ </span> : null}
        Key for: {relayReason(relay, names, network.rules)}
        {concern !== null ? `. ${concern}.` : null}
      </p>
      <dl className="node-summary">
        <DataItem
          label="Zone"
          value={
            relay.zone_id === null ? (
              "None"
            ) : (
              <ZoneTag
                zoneId={relay.zone_id}
                colors={colors}
                names={names}
                onSelect={onSelectZone}
              />
            )
          }
        />
        <DataItem label="Cuts off" value={nodeCount(relay.cuts_off)} />
        <DataItem
          label="No solid backup"
          value={nodeCount(relay.no_solid_backup)}
        />
        <DataItem label="Carries" value={nodeCount(relay.carries)} />
        <DataItem label="Neighbours" value={relay.neighbours} />
        <DataItem
          label="Last seen"
          value={hours === null ? "—" : `${formatHours(hours)} ago`}
        />
        <DataItem label="Battery" value={formatBattery(relay.battery_level)} />
        <DataItem
          label="Firmware"
          value={relay.firmware ? `≥ ${relay.firmware.minimum}` : "—"}
        />
        {network.nodes.some(
          (node) => node.node_id === nodeId && node.estimated,
        ) ? (
          <DataItem
            label="Position"
            value="Estimated from its neighbours: it reports none"
          />
        ) : null}
        <DataItem
          label="Show"
          value={
            <>
              <a className="text-button" href={mapHref(nodeId)}>
                On the Map page
              </a>{" "}
              <button
                type="button"
                className="text-button"
                onClick={() =>
                  document
                    .getElementById("key-relays-heading")
                    ?.scrollIntoView({ behavior: "smooth", block: "center" })
                }
              >
                In the table ↓
              </button>
            </>
          }
        />
      </dl>
    </section>
  );
}

/**
 * A selected zone's key relay beside the map: what it matters for, and ⚠ when
 * it was not heard lately or its battery is low. Clicking the row elsewhere
 * than its name selects the relay and centres the map on it; the name opens
 * the node's page.
 */
function RelayItem({
  relay,
  network,
  nodes,
  names,
  selected,
  onShowNode,
}: {
  relay: KeyRelay;
  network: NetworkZones;
  nodes: NodeIndex;
  names: Map<number, string>;
  selected: boolean;
  onShowNode: (nodeId: number) => void;
}) {
  const concern = relayConcern(relay, Date.parse(network.generated_at));
  const onMap = placed(nodes.get(relay.node_id)) !== null;
  return (
    <li
      data-fit-row
      className={
        [onMap ? "zones-relay-row" : "", selected ? "is-selected" : ""]
          .join(" ")
          .trim() || undefined
      }
      title={onMap ? undefined : "Not on the map: no position"}
      onClick={
        onMap
          ? (event) => {
              if ((event.target as Element).closest("a, button")) return;
              onShowNode(relay.node_id);
            }
          : undefined
      }
    >
      <span className="zones-pick-name">
        <ZoneNodeLink
          nodeId={relay.node_id}
          nodes={nodes}
          maxLength={RELAY_NAME_LENGTH}
        />
      </span>
      <span className="zones-value">
        {concern !== null ? (
          <span className="zones-late" title={concern}>
            ⚠{" "}
          </span>
        ) : null}
        {relayReason(relay, names, network.rules)}
      </span>
    </li>
  );
}

/** A pair's alternative path in words: "2 links via Murcia". */
function wayRoundText(pair: ZonePair, names: Map<number, string>): string {
  if (pair.way_round_links === null) return "none";
  const via = pair.way_round_zones.map((zoneId) => names.get(zoneId) ?? zoneId);
  return `${plural(pair.way_round_links, "link")}${via.length > 0 ? ` via ${via.join(", ")}` : ""}`;
}

/** "1 link", "3 links". */
function plural(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;
}

/** A pair link as the map and the side panel name it when it is open. */
function linkKey(link: ZonePairLink): string {
  return `${link.node_a}-${link.node_b}`;
}

/** A node's names as MapLinkDetails shows them. */
function linkEnd(nodeId: number, nodes: NodeIndex): LinkEnd {
  const node = nodes.get(nodeId);
  return {
    node_id: nodeId,
    long_name: node?.long_name ?? null,
    short_name: node?.short_name ?? null,
  };
}

type LinkSort = "solid" | "observations" | "margin" | "km";

/** A pair's links, highest first by the chosen figure; no distance goes last. */
/** Highest first; solid links first, then by times seen both ways. */
function sortLinks(links: ZonePairLink[], by: LinkSort): ZonePairLink[] {
  const value = (link: ZonePairLink) =>
    by === "solid" || by === "observations"
      ? bothWaysObservations(link)
      : by === "margin"
        ? link.weaker_margin_db
        : (link.km ?? -Infinity);
  return [...links].sort(
    (a, b) =>
      (by === "solid" ? Number(b.solid) - Number(a.solid) : 0) ||
      value(b) - value(a),
  );
}

/** A link's average SNR each way, each labelled with its direction. */
function LinkSnr({ link, nodes }: { link: ZonePairLink; nodes: NodeIndex }) {
  const a = directionEnd(link.node_a, nodes);
  const b = directionEnd(link.node_b, nodes);
  return (
    <BothWays
      left={
        <DirectionSnr
          direction={link.a_to_b}
          spreadingFactor={link.spreading_factor}
          label={`${a} → ${b}`}
        />
      }
      right={
        <DirectionSnr
          direction={link.b_to_a}
          spreadingFactor={link.spreading_factor}
          label={`${b} → ${a}`}
        />
      }
    />
  );
}

/**
 * A pair link's figures as the side panel and the pair's links table show
 * them: times seen and SNR each way, the weaker margin, and the distance
 * (between reported positions only, with line of sight).
 */
function LinkMeasurementCells({
  link,
  nodes,
}: {
  link: ZonePairLink;
  nodes: NodeIndex;
}) {
  return (
    <>
      <td className="number">
        <BothWays
          left={link.a_to_b.observations.toLocaleString()}
          right={link.b_to_a.observations.toLocaleString()}
        />
      </td>
      <td className="number">
        <LinkSnr link={link} nodes={nodes} />
      </td>
      <td>
        <MarginValue value={link.weaker_margin_db} />
      </td>
      <td className="number">
        {formatKm(link.km)}
        {link.km !== null ? (
          <a
            className="los-link"
            href={lineOfSightHref(link.node_a, link.node_b)}
            title="Line of sight"
            aria-label="Line of sight"
          >
            <Icon name="profile" />
          </a>
        ) : null}
      </td>
    </>
  );
}

/**
 * The pair selected on the map, in the table or among a zone's connections: its
 * figures in a line, then its links compact for the narrow column (zone
 * number dots and short names), in the order the pair's links table under
 * its row uses too.
 */
function PairSide({
  pair,
  nodes,
  colors,
  names,
  reach,
  sort,
  onSort,
  rows,
  openLink,
  onOpenLink,
  onSelectZone,
  backTo,
  onBack,
}: {
  pair: ZonePair;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  reach: ReachLookup;
  sort: LinkSort;
  onSort: (sort: LinkSort) => void;
  rows: number;
  openLink: string | null;
  onOpenLink: (link: string | null) => void;
  onSelectZone: (zoneId: number) => void;
  // The zone selected when the pair was, which × goes back to.
  // What its × goes back to, if not all zones.
  backTo: string | undefined;
  onBack: () => void;
}) {
  const drawn = new Set(
    drawnLinks(pair.links, nodes).map(({ link }) => linkKey(link)),
  );
  const unplaced = pair.links.length - drawn.size;
  const solid = pair.links.filter((link) => link.solid).length;
  const texts = [
    reach(pair.zone_a, pair.zone_b),
    reach(pair.zone_b, pair.zone_a),
  ] as const;
  const sorted = sortLinks(pair.links, sort);
  const links = sorted.slice(0, rows);
  // The link open on the map stays in the list, in place of the last row.
  const opened = sorted.find((link) => linkKey(link) === openLink);
  if (opened !== undefined && !links.includes(opened)) {
    links.splice(Math.max(links.length - 1, 0), 1, opened);
  }
  const node = (id: number) => (
    <a
      href={nodeHref(id)}
      title={`${nodeName(nodes.get(id)) ?? formatNodeId(id)} · ${formatNodeId(id)}`}
    >
      {shortLabel(id, nodes.get(id)?.short_name ?? null)}
    </a>
  );
  const heading = (zoneId: number) => (
    <th scope="col" title={`${zoneId}. ${names.get(zoneId) ?? "Zone"}`}>
      <ZoneNumber zoneId={zoneId} colors={colors} />
    </th>
  );
  return (
    <>
      <BackButton
        label={backTo === undefined ? "All zones" : `Back to ${backTo}`}
        onClick={onBack}
      />
      <div className="zones-zone-title">
        <h3>
          <ZoneTag
            zoneId={pair.zone_a}
            colors={colors}
            names={names}
            onSelect={onSelectZone}
          />{" "}
          ↔{" "}
          <ZoneTag
            zoneId={pair.zone_b}
            colors={colors}
            names={names}
            onSelect={onSelectZone}
          />
        </h3>
      </div>
      {/* Every figure keeps its row, with "—" when it is missing, so each is
          always in the same place; warnings come last. */}
      <dl className="zones-facts">
        <DataItem
          label="Links"
          value={
            <>
              {pair.links.length.toLocaleString()} · {solid} solid
              {unplaced > 0 ? (
                <span title="A node of each of these links has no position">
                  {" "}
                  · {unplaced} off the map
                </span>
              ) : null}
            </>
          }
        />
        <DataItem
          label="Apart"
          value={
            <>
              <span title="Between the zone centres">
                {pair.km !== null ? `${formatKm(pair.km)} km` : "—"}
              </span>
              {" · "}
              <span title="The fewest hops between the two zone hubs">
                {pair.hub_hops !== null
                  ? `${plural(pair.hub_hops, "hop")} between hubs`
                  : "no path between hubs"}
              </span>
            </>
          }
        />
        <DataItem label="Alternative path" value={wayRoundText(pair, names)} />
        <DataItem
          label="Texts"
          value={
            texts.some((figure) => figure !== null) ? (
              <span title="Share of one zone's texts the other zone heard, each way">
                <BothWays
                  left={
                    <TextReach
                      from={pair.zone_a}
                      to={pair.zone_b}
                      figure={texts[0]}
                      count
                    />
                  }
                  right={
                    <TextReach
                      from={pair.zone_b}
                      to={pair.zone_a}
                      figure={texts[1]}
                      count
                    />
                  }
                />
              </span>
            ) : (
              "—"
            )
          }
        />
        {pair.warnings.length > 0 ? (
          <DataItem
            label="Warnings"
            value={pair.warnings.map((warning, index) => (
              <Fragment key={warning}>
                {index > 0 ? " · " : null}
                <span
                  className="zones-late"
                  title={warningTitle(warning, pair, nodes)}
                >
                  {warning}
                </span>
              </Fragment>
            ))}
          />
        ) : null}
      </dl>
      <div className="zones-pair-more">
        <MoreInTable
          shown={links.length}
          total={pair.links.length}
          target={pairRowId(pair)}
        />
      </div>
      <table className="zones-table zones-pair-links zones-pair-side">
        <thead>
          <tr>
            {heading(pair.zone_a)}
            {heading(pair.zone_b)}
            <SortHeading
              label="Seen"
              title="Times heard each way"
              sort="observations"
              active={sort}
              onSort={onSort}
            />
            <th scope="col" title="Average SNR each way">
              SNR
            </th>
            <SortHeading
              label="Margin"
              title="Lowest of both directions: dB above the weakest signal the channel can still decode"
              sort="margin"
              active={sort}
              onSort={onSort}
            />
            <SortHeading label="km" sort="km" active={sort} onSort={onSort} />
            <SortHeading
              label="Solid"
              title="Solid links first"
              sort="solid"
              active={sort}
              onSort={onSort}
            />
          </tr>
        </thead>
        <tbody>
          {links.map((link) => {
            const key = linkKey(link);
            // A link the map cannot draw has nothing to open there.
            const placed = drawn.has(key);
            return (
              <tr
                key={key}
                data-fit-row
                className={
                  placed
                    ? `zones-pair-row${key === openLink ? " is-selected" : ""}`
                    : "zones-unplaced-row"
                }
                title={
                  placed ? undefined : "Not on the map: a node has no position"
                }
                onClick={
                  placed
                    ? (event) => {
                        // Node and line of sight links keep their own behaviour.
                        if ((event.target as Element).closest("a")) return;
                        onOpenLink(key === openLink ? null : key);
                      }
                    : undefined
                }
              >
                <td>{node(link.node_a)}</td>
                <td>{node(link.node_b)}</td>
                <LinkMeasurementCells link={link} nodes={nodes} />
                <td>
                  {link.solid ? (
                    <span className="zones-solid-mark" title="Solid link">
                      ✓
                    </span>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}

/**
 * A selected zone: its facts, the zones it joins (most links first; a row
 * selects the pair) and its key relays. When they do not all fit, the key
 * relays are cut first.
 */
function ZoneDetails({
  zone,
  joins,
  relays,
  network,
  nodes,
  colors,
  names,
  rows,
  onSelectPair,
  selectedRelay,
  onShowNode,
  onBack,
}: {
  zone: Zone;
  joins: ZonePair[];
  relays: KeyRelay[];
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  rows: number;
  onSelectPair: (pair: string) => void;
  selectedRelay: number | null;
  onShowNode: (nodeId: number) => void;
  onBack: () => void;
}) {
  const placed = zone.members.filter(
    (id) => nodes.get(id)?.latitude != null,
  ).length;
  const shownJoins = joins.slice(0, rows);
  const shownRelays = relays.slice(0, Math.max(0, rows - joins.length));
  // The other zones of its island, and those its solid paths on the map reach.
  const onPaths = new Set(zone.solid_paths.flat());
  const others = network.zones.filter(
    (other) => other.island === zone.island && other.zone_id !== zone.zone_id,
  );
  const reached = others.filter((other) =>
    other.members.some((id) => onPaths.has(id)),
  ).length;
  return (
    <>
      <BackButton label="All zones" onClick={onBack} />
      <div className="zones-zone-title">
        <h3>
          <ZoneSwatch color={colors.get(zone.zone_id)} />
          <span>
            {zone.zone_id}. {names.get(zone.zone_id) ?? zone.name}
            {zone.place !== null && zone.region ? (
              <span className="muted">, {zone.region}</span>
            ) : null}
          </span>
        </h3>
      </div>
      <dl className="zones-facts">
        <DataItem
          label="Members"
          value={`${zone.members.length} · ${placed} with a position`}
        />
        <DataItem label="Links inside" value={zone.internal_links} />
        <DataItem
          label="Hub"
          value={<ZoneNodeLink nodeId={zone.hub_node_id} nodes={nodes} />}
        />
        <DataItem
          label="Texts"
          value={`${plural(zone.text_senders, "sender")} · ${plural(zone.text_count, "text")} · ${plural(zone.text_gateways, "gateway")}`}
        />
        {others.length > 0 ? (
          <DataItem
            label="Solid paths"
            value={`to ${reached} of ${plural(others.length, "zone")}, drawn on the map`}
          />
        ) : null}
      </dl>
      <div className="zones-side-heading">
        <h4>Connections ({joins.length})</h4>
        <MoreInTable
          shown={shownJoins.length}
          total={joins.length}
          target={joins.length > 0 ? "between-zones-heading" : undefined}
        />
      </div>
      {joins.length === 0 ? (
        <p className="empty-inline">None.</p>
      ) : (
        <ul className="zones-pick-list">
          {shownJoins.map((pair) => {
            const other =
              pair.zone_a === zone.zone_id ? pair.zone_b : pair.zone_a;
            const solid = pair.links.filter((link) => link.solid).length;
            const warning = pair.warnings.find((item) =>
              SOLID_WARNINGS.includes(item),
            );
            return (
              <li key={pairKey(pair)} data-fit-row>
                <button
                  type="button"
                  title={`Best link's signal margin${warning === undefined ? "" : `. ${warningTitle(warning, pair, nodes) ?? warning}`}`}
                  onClick={() => onSelectPair(pairKey(pair))}
                >
                  <span className="zones-pick-name">
                    <TierDot
                      color={linkTierColor(
                        marginTier(pair.strongest_margin_db),
                      )}
                    />
                    <ZoneNumber zoneId={other} colors={colors} />
                    <span className="zones-pick-text">{names.get(other)}</span>
                  </span>
                  <span className="zones-value">
                    {plural(pair.links.length, "link")} ·{" "}
                    {warning === undefined ? (
                      `${solid} solid`
                    ) : (
                      <span className="zones-late">{warning}</span>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <div className="zones-side-heading">
        <h4>Key relays ({relays.length})</h4>
        <MoreInTable
          shown={shownRelays.length}
          total={relays.length}
          target={relays.length > 0 ? "key-relays-heading" : undefined}
        />
      </div>
      {relays.length === 0 ? (
        <p className="empty-inline">None.</p>
      ) : (
        <ul className="zones-list">
          {shownRelays.map((relay) => (
            <RelayItem
              key={relay.node_id}
              relay={relay}
              network={network}
              nodes={nodes}
              names={names}
              selected={relay.node_id === selectedRelay}
              onShowNode={onShowNode}
            />
          ))}
        </ul>
      )}
    </>
  );
}

// ------------------------------------------------------------ Between zones

type PairSort =
  | "warnings"
  | "zones"
  | "links"
  | "texts"
  | "observations"
  | "margin"
  | "hub_hops"
  | "way_round"
  | "km"
  | "solid";

interface PairOrder {
  by: PairSort;
  descending: boolean;
}

/**
 * The figure a zone pair is sorted by; null (no hub route, no text figure)
 * always goes last.
 */
function pairSortValue(
  pair: ZonePair,
  by: PairSort,
  reach: ReachLookup,
): number | null {
  switch (by) {
    case "zones":
      // Zone ids are small, so this orders by the first zone, then the second.
      return pair.zone_a * 100_000 + pair.zone_b;
    case "links":
      return pair.links.length;
    case "texts":
      return worseTextShare(pair, reach);
    case "margin":
      // The pair's best link: the one the "all weak" warning looks at.
      return pair.strongest_margin_db;
    case "observations":
      return pair.observations;
    case "hub_hops":
      return pair.hub_hops;
    case "way_round":
      return pair.way_round_links;
    case "km":
      return pair.km;
    case "solid":
      return pair.links.filter((link) => link.solid).length;
    case "warnings":
      return null;
  }
}

/**
 * Zone pairs in the chosen order. Warnings descending is weakest first, the
 * order zones.py lists them in; every other column keeps that among ties.
 */
function sortPairs(
  pairs: ZonePair[],
  order: PairOrder,
  reach: ReachLookup,
): ZonePair[] {
  const weakest = [...pairs];
  if (order.by === "warnings") {
    return order.descending ? weakest : weakest.reverse();
  }
  const sign = order.descending ? -1 : 1;
  return weakest.sort((a, b) => {
    const x = pairSortValue(a, order.by, reach);
    const y = pairSortValue(b, order.by, reach);
    if (x === null || y === null)
      return (x === null ? 1 : 0) - (y === null ? 1 : 0);
    return sign * (x - y);
  });
}

function BetweenZones({
  network,
  nodes,
  colors,
  names,
  selectedZone,
  selectedPair,
  onSelectZone,
  onSelectPair,
  linkSort,
  onLinkSort,
}: {
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  selectedZone: number | null;
  selectedPair: string | null;
  onSelectZone: (zoneId: number | null) => void;
  onSelectPair: (pair: string | null) => void;
  linkSort: LinkSort;
  onLinkSort: (sort: LinkSort) => void;
}) {
  const [showAll, setShowAll] = useViewState("zones.allPairs", false);
  const [order, setOrder] = useSetting<PairOrder>(
    "zones.pairOrder",
    { by: "km", descending: true },
    (value) => typeof (value as PairOrder | null)?.by === "string",
  );
  // Zones under their shown names, for the heading and the link tables.
  const zones = useMemo(
    () =>
      new Map(
        network.zones.map((zone) => [
          zone.zone_id,
          { ...zone, name: names.get(zone.zone_id) ?? zone.name },
        ]),
      ),
    [network, names],
  );
  const reach = useMemo(() => textReachLookup(network), [network]);
  const ordered = useMemo(
    () => sortPairs(network.zone_pairs, order, reach),
    [network, order, reach],
  );
  // A pair chosen on the map stays in the table whatever the zone and rows.
  const matching = ordered.filter(
    (pair) =>
      selectedZone === null ||
      pair.zone_a === selectedZone ||
      pair.zone_b === selectedZone ||
      pairKey(pair) === selectedPair,
  );
  const rows = showAll ? matching : matching.slice(0, PAIR_ROWS);
  const picked = matching.find((pair) => pairKey(pair) === selectedPair);
  if (picked !== undefined && !rows.includes(picked)) rows.unshift(picked);
  const zone = selectedZone === null ? undefined : zones.get(selectedZone);

  // A second click on the sorted column flips it; zones start lowest first,
  // figures highest first.
  function sortBy(by: PairSort) {
    setOrder((current) =>
      current.by === by
        ? { by, descending: !current.descending }
        : { by, descending: by !== "zones" },
    );
  }
  const heading = (
    label: string,
    by: PairSort,
    note?: string,
    title?: string,
  ) => (
    <SortHeading
      label={label}
      note={note}
      title={title}
      sort={by}
      active={order.by}
      descending={order.descending}
      onSort={sortBy}
    />
  );

  // Opening or closing a pair also swaps the column beside the map, above this
  // table, for one of another height, and while the sticky map is on screen
  // the browser does not make up for it. So the clicked row is put back where
  // it was on screen before the change is painted.
  const clicked = useRef<{ id: string; top: number } | null>(null);
  useLayoutEffect(() => {
    const pin = clicked.current;
    clicked.current = null;
    if (pin === null) return;
    const row = document.getElementById(pin.id);
    // The page scrolls inside the app's content area (App.tsx), not the window.
    const page = row?.closest(".app-content");
    if (!row || !page) return;
    page.scrollTop += row.getBoundingClientRect().top - pin.top;
  }, [selectedPair]);

  function toggle(event: MouseEvent, pair: ZonePair) {
    // Zone buttons and node links in the row keep their own behaviour.
    if ((event.target as Element).closest("a, button:not(.zones-toggle)")) {
      return;
    }
    clicked.current = {
      id: event.currentTarget.id,
      top: event.currentTarget.getBoundingClientRect().top,
    };
    onSelectPair(pairKey(pair) === selectedPair ? null : pairKey(pair));
  }

  return (
    <section aria-labelledby="between-zones-heading">
      <div className="zones-section-heading">
        <h3 id="between-zones-heading">
          Between zones
          {zone !== undefined ? ` touching ${zone.zone_id}. ${zone.name}` : ""}
        </h3>
        {zone !== undefined ? (
          <IconButton
            icon="close"
            label="Show all zones"
            onClick={() => onSelectZone(null)}
          />
        ) : null}
        <ShowAll
          total={matching.length}
          limit={PAIR_ROWS}
          showAll={showAll}
          onChange={setShowAll}
        />
      </div>
      {rows.length === 0 ? (
        <p className="empty-inline">No links between zones.</p>
      ) : (
        <WideTable>
          <table className="zones-table zones-between">
            <thead>
              <tr>
                {heading(
                  "Zones",
                  "zones",
                  undefined,
                  "Pairs of zones that can reach each other. Select a row for its links.",
                )}
                {heading("Warnings", "warnings")}
                {heading(
                  "Solid links",
                  "solid",
                  undefined,
                  "Direct links between the zones that meet the solid link setting above",
                )}
                {heading("Links", "links")}
                {heading(
                  "Texts",
                  "texts",
                  "reached",
                  "Share of one zone's texts that the other zone also heard, each way.",
                )}
                {heading("Seen", "observations", "times")}
                {heading(
                  "Margin",
                  "margin",
                  "strongest link",
                  "Signal margin, weaker direction",
                )}
                {heading(
                  "Between hubs",
                  "hub_hops",
                  "hops",
                  "The fewest hops between the two zone hubs",
                )}
                {heading("Alternative path", "way_round", "solid links")}
                {heading("Apart", "km", "km", "Between the zone centres")}
                <th
                  scope="col"
                  title="How many different nodes carry the links on each side"
                >
                  Carried by
                  <span className="heading-note">nodes each side</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((pair) => {
                const open = pairKey(pair) === selectedPair;
                const warnings = pair.warnings;
                const direct = isDirect(pair);
                const none = <span className="muted">—</span>;
                // The selected zone goes first, whatever its number.
                const flip = pair.zone_b === selectedZone;
                const [first, second] = flip
                  ? [pair.zone_b, pair.zone_a]
                  : [pair.zone_a, pair.zone_b];
                const [carriedFirst, carriedSecond] = flip
                  ? [pair.through_nodes_b, pair.through_nodes_a]
                  : [pair.through_nodes_a, pair.through_nodes_b];
                return (
                  <Fragment key={pairKey(pair)}>
                    <tr
                      id={pairRowId(pair)}
                      className={`zones-pair-row${open ? " is-selected" : ""}`}
                      onClick={(event) => toggle(event, pair)}
                    >
                      <td>
                        <ZoneChip
                          zoneId={first}
                          colors={colors}
                          names={names}
                          onSelect={onSelectZone}
                        />{" "}
                        ↔{" "}
                        <ZoneChip
                          zoneId={second}
                          colors={colors}
                          names={names}
                          onSelect={onSelectZone}
                        />
                      </td>
                      <td>
                        {warnings.length === 0 ? (
                          <span className="muted">—</span>
                        ) : (
                          <span className="zones-badges">
                            {warnings.map((warning) => (
                              <Badge
                                key={warning}
                                tone="amber"
                                title={warningTitle(warning, pair, nodes)}
                              >
                                {warning}
                              </Badge>
                            ))}
                          </span>
                        )}
                      </td>
                      <td className="number">
                        {direct
                          ? pair.links.filter((link) => link.solid).length
                          : none}
                      </td>
                      <td
                        className="number"
                        title={
                          direct
                            ? undefined
                            : "No link directly between these zones: they are joined only through other zones"
                        }
                      >
                        {direct ? (
                          <button
                            type="button"
                            className="zones-toggle"
                            aria-expanded={open}
                            aria-label={`${open ? "Hide" : "Show"} the links between zones ${first} and ${second}`}
                          >
                            {open ? "▾" : "▸"} {pair.links.length}
                          </button>
                        ) : (
                          <span className="muted">none</span>
                        )}
                      </td>
                      <td>
                        <BothWays
                          left={
                            <TextReach
                              from={first}
                              to={second}
                              figure={reach(first, second)}
                            />
                          }
                          right={
                            <TextReach
                              from={second}
                              to={first}
                              figure={reach(second, first)}
                            />
                          }
                        />
                      </td>
                      <td className="number">
                        {direct ? pair.observations.toLocaleString() : none}
                      </td>
                      <td>
                        {pair.strongest_margin_db === null ? (
                          none
                        ) : (
                          <MarginValue value={pair.strongest_margin_db} />
                        )}
                      </td>
                      <td className="number">{pair.hub_hops ?? "—"}</td>
                      <td
                        title={`Alternative path: ${wayRoundText(pair, names)}`}
                      >
                        {pair.way_round_links === null ? (
                          <span className="muted">—</span>
                        ) : (
                          <span className="zones-relay-zone">
                            {pair.way_round_links}
                            {pair.way_round_zones.map((zoneId) => (
                              <ZoneNumber
                                key={zoneId}
                                zoneId={zoneId}
                                colors={colors}
                              />
                            ))}
                          </span>
                        )}
                      </td>
                      <td className="number">{formatKm(pair.km)}</td>
                      <td className="number">
                        {direct ? `${carriedFirst} · ${carriedSecond}` : none}
                      </td>
                    </tr>
                    {open && direct ? (
                      <tr className="zones-pair-detail">
                        <td colSpan={11}>
                          <PairLinks
                            pair={pair}
                            zones={zones}
                            nodes={nodes}
                            network={network}
                            sort={linkSort}
                            onSort={onLinkSort}
                          />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </WideTable>
      )}
    </section>
  );
}

/** What a pair warning means, for its badge's tooltip. */
function warningTitle(
  warning: PairWarning,
  pair: ZonePair,
  nodes: NodeIndex,
): string | undefined {
  if (warning === "no solid route") {
    return "No solid path joins these zones, directly or through other zones.";
  }
  if (warning === "one solid link") {
    return "If either end goes quiet, no solid link joins these zones.";
  }
  if (warning === "one relay") {
    const relay = pair.sole_relay;
    const solid = pair.links.filter((link) => link.solid).length;
    const name = relay === null ? "one node" : directionEnd(relay, nodes);
    return `All ${solid} solid links pass through ${name}: if it goes quiet, no solid link joins these zones.`;
  }
  return undefined;
}

function pairRowId(pair: ZonePair): string {
  return `zone-pair-${pairKey(pair)}`;
}

/**
 * Each link between two zones, from zone_a's side to zone_b's; a row opens its
 * details over the page's window, as on the Links page.
 */
function PairLinks({
  pair,
  zones,
  nodes,
  network,
  sort,
  onSort,
}: {
  pair: ZonePair;
  zones: Map<number, Zone>;
  nodes: NodeIndex;
  network: NetworkZones;
  sort: LinkSort;
  onSort: (sort: LinkSort) => void;
}) {
  const [open, setOpen] = useViewState<string | null>("zones.openRow", null);
  const since = new Date(
    Date.parse(network.generated_at) - network.window_hours * HOUR_MS,
  ).toISOString();
  const side = (zoneId: number) =>
    `${zoneId}. ${zones.get(zoneId)?.name ?? "Zone"} side`;
  return (
    <table className="zones-table zones-pair-links">
      <thead>
        <tr>
          <th scope="col">{side(pair.zone_a)}</th>
          <th scope="col">{side(pair.zone_b)}</th>
          <th scope="col">Explorer</th>
          <SortHeading
            label="Seen"
            note="times"
            title="Times heard each way"
            sort="observations"
            active={sort}
            onSort={onSort}
          />
          <th scope="col" title="Average SNR each way">
            SNR<span className="heading-note">dB</span>
          </th>
          <SortHeading
            label="Margin"
            note="lowest"
            title="Lowest of both directions: dB above the weakest signal the channel can still decode"
            sort="margin"
            active={sort}
            onSort={onSort}
          />
          <SortHeading
            label="Distance"
            note="km"
            sort="km"
            active={sort}
            onSort={onSort}
          />
          <SortHeading
            label="Solid"
            title="Solid links first"
            sort="solid"
            active={sort}
            onSort={onSort}
          />
          <th scope="col">Details</th>
        </tr>
      </thead>
      <tbody>
        {sortLinks(pair.links, sort).map((link) => {
          const key = linkKey(link);
          const expanded = open === key;
          const toggle = () => setOpen(expanded ? null : key);
          return (
            <Fragment key={key}>
              <tr
                className="clickable-row"
                onClick={(event) => {
                  if ((event.target as Element).closest("a, button")) return;
                  toggle();
                }}
              >
                <td>
                  <ZoneNodeLink nodeId={link.node_a} nodes={nodes} />
                </td>
                <td>
                  <ZoneNodeLink nodeId={link.node_b} nodes={nodes} />
                </td>
                <td>
                  <a
                    className="icon-button"
                    href={tracerouteHopHref(link.node_a, link.node_b)}
                    title="All traceroutes with this hop"
                    aria-label="All traceroutes with this hop"
                  >
                    <Icon name="route" />
                  </a>
                </td>
                <LinkMeasurementCells link={link} nodes={nodes} />
                <td>
                  <span className="zones-badges">
                    {link.solid ? <Badge tone="green">solid</Badge> : null}
                  </span>
                </td>
                <td className="expand-cell">
                  <IconButton
                    icon="chevron"
                    label={
                      expanded
                        ? "Hide this link's details"
                        : "Show this link's details"
                    }
                    aria-expanded={expanded}
                    onClick={toggle}
                  />
                </td>
              </tr>
              {expanded ? (
                <tr className="links-expanded">
                  <td colSpan={9}>
                    <LinkDetails
                      link={{ ...link, channel: network.channel ?? "" }}
                      linkKind="traceroute"
                      nodes={nodes}
                      since={since}
                    />
                  </td>
                </tr>
              ) : null}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

// ------------------------------------------------------------ Key relays

type RelaySort =
  | "node"
  | "role"
  | "zone"
  | "only_solid_link_to"
  | "cuts_off"
  | "no_solid_backup"
  | "carries"
  | "neighbours"
  | "last_seen"
  | "battery"
  | "firmware";

// Columns of words start lowest first; figures start highest first.
const RELAY_TEXT_SORTS: RelaySort[] = ["node", "role", "zone"];

/** What a key relay is sorted by: for only solid links, the number of zones. */
function relaySortValue(
  relay: KeyRelay,
  sort: RelaySort,
  nodes: NodeIndex,
): number | string | null {
  switch (sort) {
    case "node":
      return directionEnd(relay.node_id, nodes).toLowerCase();
    case "role":
      return formatRole(nodes.get(relay.node_id)?.role ?? null);
    case "zone":
      return relay.zone_id;
    case "only_solid_link_to":
      return relay.only_solid_link_to.length;
    case "last_seen":
      return relay.last_seen_at && new Date(relay.last_seen_at).getTime();
    case "battery":
      return relay.battery_level;
    case "firmware":
      return relay.firmware?.minimum ?? null;
    default:
      return relay[sort];
  }
}

/** Key relays in the given order; ones with nothing to sort by go last. */
function sortRelays(
  relays: KeyRelay[],
  order: { by: RelaySort; descending: boolean },
  nodes: NodeIndex,
): KeyRelay[] {
  const sign = order.descending ? -1 : 1;
  return [...relays].sort((a, b) => {
    const x = relaySortValue(a, order.by, nodes);
    const y = relaySortValue(b, order.by, nodes);
    if (x === null || y === null)
      return (x === null ? 1 : 0) - (y === null ? 1 : 0);
    // Firmware versions compare number by number, so 2.10 comes after 2.9.
    if (typeof x === "string" || typeof y === "string")
      return (
        sign * String(x).localeCompare(String(y), undefined, { numeric: true })
      );
    return sign * (x - y);
  });
}

function KeyRelays({
  network,
  nodes,
  colors,
  names,
  selectedZone,
  onSelectZone,
  selectedRelay,
}: {
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  selectedZone: number | null;
  onSelectZone: (zoneId: number | null) => void;
  selectedRelay: number | null;
}) {
  const [order, setOrder] = useSetting<{ by: RelaySort; descending: boolean }>(
    "zones.relayOrder",
    { by: "cuts_off", descending: true },
    (value) => typeof (value as { by?: unknown } | null)?.by === "string",
  );
  const [showAll, setShowAll] = useViewState("zones.allRelays", false);
  // The API lists them by impact; a stable sort keeps that order among ties.
  // With a zone selected, only its own.
  const sorted = useMemo(
    () =>
      sortRelays(
        network.key_relays.filter(
          (relay) => selectedZone === null || relay.zone_id === selectedZone,
        ),
        order,
        nodes,
      ),
    [network, order, nodes, selectedZone],
  );
  // A second click on the sorted column flips it.
  function sortBy(by: RelaySort) {
    setOrder((current) =>
      current.by === by
        ? { by, descending: !current.descending }
        : { by, descending: !RELAY_TEXT_SORTS.includes(by) },
    );
  }
  const rows = showAll ? sorted : sorted.slice(0, RELAY_ROWS);
  const now = new Date(network.generated_at).getTime();
  const heading = (
    label: string,
    key: RelaySort,
    note?: string,
    title?: string,
  ) => (
    <SortHeading
      label={label}
      note={note}
      title={title}
      sort={key}
      active={order.by}
      descending={order.descending}
      onSort={sortBy}
    />
  );
  return (
    <section aria-labelledby="key-relays-heading">
      <div className="zones-section-heading">
        <h3 id="key-relays-heading">
          Key relays
          {selectedZone !== null
            ? ` in ${selectedZone}. ${names.get(selectedZone) ?? ""}`
            : ""}
        </h3>
        {selectedZone !== null ? (
          <IconButton
            icon="close"
            label="Show all zones"
            onClick={() => onSelectZone(null)}
          />
        ) : null}
        <ShowAll
          total={sorted.length}
          limit={RELAY_ROWS}
          showAll={showAll}
          onChange={setShowAll}
        />
      </div>
      {rows.length === 0 ? (
        <p className="empty-inline">
          No key relays{" "}
          {selectedZone !== null ? "in this zone" : "in this window"}.
        </p>
      ) : (
        <WideTable>
          <table className="zones-table zones-relays">
            <thead>
              <tr>
                {heading(
                  "Node",
                  "node",
                  undefined,
                  "Relays the network depends on. Their map marker is shown beside them.",
                )}
                {heading("Role", "role")}
                {heading("Zone", "zone")}
                {heading(
                  "Only solid link to",
                  "only_solid_link_to",
                  undefined,
                  "Zones that lose their solid link if this relay goes quiet",
                )}
                {heading(
                  "Cuts off",
                  "cuts_off",
                  "nodes",
                  "The nodes that lose every path to the rest of the network if this one goes quiet",
                )}
                {heading(
                  "No solid backup",
                  "no_solid_backup",
                  "nodes",
                  "The nodes that lose every solid path to the rest of the network if this one goes quiet",
                )}
                {heading(
                  "Carries",
                  "carries",
                  "nodes",
                  "Nodes that usually reach the main zones through this relay",
                )}
                {heading(
                  "Neighbours",
                  "neighbours",
                  undefined,
                  "Traceroute Neighbours, observed 2 times or more",
                )}
                {heading("Last seen", "last_seen")}
                {heading("Battery", "battery")}
                {heading("Firmware", "firmware", undefined, "Minimum firmware")}
              </tr>
            </thead>
            <tbody>
              {rows.map((relay) => {
                const node = nodes.get(relay.node_id);
                const hours = hoursSince(relay.last_seen_at, now);
                return (
                  <tr
                    key={relay.node_id}
                    className={
                      relay.node_id === selectedRelay
                        ? "is-selected"
                        : undefined
                    }
                  >
                    <td>
                      {holdsOnlySolidLink(relay) ? (
                        <RelayMark filled />
                      ) : isOutlined(relay, network.rules) ? (
                        <RelayMark filled={false} />
                      ) : null}
                      <ZoneNodeLink
                        nodeId={relay.node_id}
                        nodes={nodes}
                        maxLength={RELAY_NAME_LENGTH}
                      />
                      {node?.latitude != null ? (
                        <a
                          className="zones-map-link"
                          href={mapHref(relay.node_id)}
                          title="Show on the map"
                          aria-label="Show on the map"
                        >
                          <Icon name="pin" />
                        </a>
                      ) : null}
                    </td>
                    <td>{formatRole(node?.role ?? null)}</td>
                    <td>
                      {relay.zone_id === null ? (
                        "—"
                      ) : (
                        <ZoneTag
                          zoneId={relay.zone_id}
                          colors={colors}
                          names={names}
                          onSelect={onSelectZone}
                        />
                      )}
                    </td>
                    <td>
                      {relay.only_solid_link_to.length === 0 ? (
                        "—"
                      ) : (
                        <span className="zones-relay-zones">
                          {relay.only_solid_link_to.map((zoneId) => (
                            <ZoneTag
                              key={zoneId}
                              zoneId={zoneId}
                              colors={colors}
                              names={names}
                            />
                          ))}
                        </span>
                      )}
                    </td>
                    <td className="number">{relay.cuts_off}</td>
                    <td className="number">{relay.no_solid_backup}</td>
                    <td className="number">{relay.carries}</td>
                    <td className="number">{relay.neighbours}</td>
                    <td
                      className={
                        hours !== null && hours > LATE_HOURS
                          ? "zones-late"
                          : undefined
                      }
                      title={
                        relay.last_seen_at
                          ? new Date(relay.last_seen_at).toLocaleString()
                          : undefined
                      }
                    >
                      {hours === null ? "—" : `${formatHours(hours)} ago`}
                    </td>
                    <td
                      className={
                        relay.battery_level != null &&
                        relay.battery_level <= LOW_BATTERY
                          ? "number zones-late"
                          : "number"
                      }
                      title={
                        relay.battery_observed_at
                          ? `Reported ${new Date(relay.battery_observed_at).toLocaleString()}`
                          : undefined
                      }
                    >
                      {formatBattery(relay.battery_level)}
                    </td>
                    <td
                      title={
                        relay.firmware
                          ? `${relay.firmware.minimum} or newer: ${relay.firmware.reason}`
                          : undefined
                      }
                    >
                      {relay.firmware ? `≥ ${relay.firmware.minimum}` : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </WideTable>
      )}
    </section>
  );
}

/**
 * A zone's number on its colour, then its name; with onSelect, a button that
 * selects the zone.
 */
function ZoneTag({
  zoneId,
  colors,
  names,
  onSelect,
}: {
  zoneId: number;
  colors: Map<number, string>;
  names: Map<number, string>;
  onSelect?: (zoneId: number) => void;
}) {
  const content = (
    <>
      <ZoneNumber zoneId={zoneId} colors={colors} />
      {names.get(zoneId)}
    </>
  );
  if (onSelect === undefined) {
    return <span className="zones-relay-zone">{content}</span>;
  }
  return (
    <button
      type="button"
      className="zones-zone-chip zones-relay-zone"
      title={`Select zone ${zoneId}`}
      onClick={() => onSelect(zoneId)}
    >
      {content}
    </button>
  );
}

/** A zone's number on its colour. */
function ZoneNumber({
  zoneId,
  colors,
}: {
  zoneId: number;
  colors: Map<number, string>;
}) {
  return (
    <span
      className="zones-number-dot"
      style={{ background: colors.get(zoneId) }}
    >
      {zoneId}
    </span>
  );
}

// ------------------------------------------------------------ Shared pieces

function ShowAll({
  total,
  limit,
  showAll,
  onChange,
}: {
  total: number;
  limit: number;
  showAll: boolean;
  onChange: (showAll: boolean) => void;
}) {
  if (total <= limit) return null;
  return (
    <span className="zones-show-all">
      Showing {showAll ? total.toLocaleString() : limit} of{" "}
      {total.toLocaleString()}.{" "}
      <button
        type="button"
        className="secondary-button"
        onClick={() => onChange(!showAll)}
      >
        {showAll ? "Show fewer" : "Show all"}
      </button>
    </span>
  );
}

/** A node's link; a name longer than maxLength is cut, whole in the tooltip. */
function ZoneNodeLink({
  nodeId,
  nodes,
  maxLength,
}: {
  nodeId: number;
  nodes: NodeIndex;
  maxLength?: number;
}) {
  const node = nodes.get(nodeId);
  const name = nodeName(node);
  const cut =
    name !== null && maxLength !== undefined && name.length > maxLength;
  return (
    <NodeLink
      nodeId={nodeId}
      name={cut ? `${name.slice(0, maxLength - 1)}…` : name}
      title={`${cut ? `${name} · ` : ""}${formatNodeId(nodeId)}${node?.short_name ? ` · ${node.short_name}` : ""}`}
    />
  );
}

/**
 * A zone's swatch and name as a button that selects the zone: its details
 * beside the map and only its rows in the tables.
 */
function ZoneChip({
  zoneId,
  colors,
  names,
  title,
  onSelect,
}: {
  zoneId: number;
  colors: Map<number, string>;
  names: Map<number, string>;
  title?: string;
  onSelect: (zoneId: number) => void;
}) {
  return (
    <button
      type="button"
      className="zones-zone-chip"
      title={title ?? `Select zone ${zoneId}`}
      onClick={() => onSelect(zoneId)}
    >
      <ZoneSwatch color={colors.get(zoneId)} />
      {zoneId}. {names.get(zoneId) ?? "?"}
    </button>
  );
}

function ZoneSwatch({ color }: { color: string | undefined }) {
  return (
    <span
      className="zones-swatch"
      style={{ background: color }}
      aria-hidden="true"
    />
  );
}

/**
 * One direction of text reach between two zones, one side of the Texts
 * column's "94% ⇄ 40%": the left figure is the row's left zone to its right
 * zone. The direction and the counts behind the figure are in the tooltip.
 */
function TextReach({
  from,
  to,
  figure,
  count = false,
}: {
  from: number;
  to: number;
  figure: TextReachFigure;
  // Whether to show how many texts the share is of, beside it.
  count?: boolean;
}) {
  const direction = `${from} → ${to}`;
  if (figure === null || figure === "no gateway") {
    return (
      <span
        className="muted"
        title={`${direction}: ${figure === null ? `no texts from zone ${from}` : `no gateway in zone ${to}`}`}
      >
        —
      </span>
    );
  }
  return (
    <span
      className="zones-margin"
      title={`${direction}: ${figure.senders} sender${figure.senders === 1 ? "" : "s"}, ${figure.texts.toLocaleString()} texts`}
    >
      <TierDot color={SHARE_COLORS[tierOfShare(figure.share)]} />
      {formatShare(figure.share)}
      {count ? (
        <span className="muted"> · {plural(figure.texts, "text")}</span>
      ) : null}
    </span>
  );
}

/** A fade margin with the map's tier colour beside it. */
function directionEnd(nodeId: number, nodes: NodeIndex): string {
  return nodeName(nodes.get(nodeId)) ?? formatNodeId(nodeId);
}

/** A battery level as "87%", "powered" for 101 (external power), or "—". */
function formatBattery(level: number | null): string {
  if (level === null) return "—";
  return level > 100 ? "powered" : `${level}%`;
}
