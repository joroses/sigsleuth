import {
  Fragment,
  type MouseEvent,
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
  solidPathLinks,
  textReachLookup,
  worseTextShare,
  zoneColors,
  zoneName,
} from "./zones";
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
import { SHARE_COLORS, tierOfShare } from "./mapNodeLayers";
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
  Badge,
  BothWays,
  CopyValue,
  DataItem,
  DirectionSnr,
  ErrorPanel,
  Icon,
  IconButton,
  MarginValue,
  NodeLink,
  SidebarNav,
  SidebarTabs,
  SortHeading,
  StatusPanel,
  TierDot,
  WideTable,
} from "./ui";
import { openedAt, useApi } from "./useApi";

// The Zones page: zones of well-linked nodes, how strongly they are joined,
// and the relays everything depends on. The backend (zones.py) does the graph
// work; this page lays it out.

const WINDOW_LABELS: Record<ZoneWindow, string> = {
  24: "24 hours",
  72: "3 days",
  168: "7 days",
};
const PER_DAY_CHOICES = [1, 2, 3, 5];
const MARGIN_CHOICES = [0, 2, 4, 6, 10];
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

// The side panel's lists, and a selected zone's.
type ListTab = "zones" | "relays";
type ZoneTab = "connections" | "relays";
const STEP_NOUNS = {
  zone: "zone",
  pair: "connection",
  link: "link",
  relay: "key relay",
} as const;

// Opens and closes the build steps and the legend together.
const PANEL_OPEN_KEY = "zonesLegendOpen";
// A moved slider asks for new data once it has rested this long, so dragging
// across several steps reloads the page once.
const SLIDER_SETTLE_MS = 400;

const DEFAULT_REQUEST: NetworkZonesRequest = {
  windowHours: 72,
  channel: null,
  perDay: 2,
  minMarginDb: 4,
};

// The last window, channel and solid-link rule are kept together; a value no
// longer offered puts them all back to the default.
function validRequest(value: unknown): boolean {
  const request = value as NetworkZonesRequest;
  return (
    ZONE_WINDOWS.includes(request.windowHours) &&
    PER_DAY_CHOICES.includes(request.perDay) &&
    MARGIN_CHOICES.includes(request.minMarginDb) &&
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
  const [listTab, setListTab] = useSetting<ListTab>(
    "zones.listTab",
    "zones",
    (value) => value === "zones" || value === "relays",
  );
  const [zoneTab, setZoneTab] = useSetting<ZoneTab>(
    "zones.zoneTab",
    "connections",
    (value) => value === "connections" || value === "relays",
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
  }

  // The side panel shows the latest choice, so a zone or pair replaces an
  // open link or key relay.
  function selectZone(zoneId: number | null) {
    setSelectedZone(zoneId);
    setSelectedPair(null);
    setOpenLink(null);
    setSelectedRelay(null);
  }

  function selectPair(key: string | null) {
    setSelectedPair(key);
    setOpenLink(null);
    setSelectedRelay(null);
  }

  // A zone row beside the map selects the zone and fits the map to its
  // members, leaving out lone ones as its shaded area does.
  function selectAndShowZone(zoneId: number) {
    selectZone(zoneId);
    const members = shown?.zones.find((item) => item.zone_id === zoneId);
    const points = (members?.members ?? []).flatMap((id) => {
      const point = placed(nodes.get(id));
      return point === null ? [] : [point];
    });
    const area = members?.hull ?? [];
    setFocus({ points: area.length > 0 ? area : points });
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
  // Those with a warning first, as they are the ones to go and look at; the
  // stable sort keeps the impact order within each.
  const generatedAt = Date.parse(shown?.generated_at ?? "");
  const relays = [...(shown?.key_relays ?? [])].sort(
    (a, b) =>
      Number(relayConcern(b, generatedAt) !== null) -
      Number(relayConcern(a, generatedAt) !== null),
  );
  const warningRelays = relays.filter(
    (relay) => relayConcern(relay, generatedAt) !== null,
  );
  // A selected zone's connections, most links first, and its key relays.
  const joins = (shown?.zone_pairs ?? [])
    .filter(
      (item) =>
        isDirect(item) &&
        (item.zone_a === zone?.zone_id || item.zone_b === zone?.zone_id),
    )
    .sort((a, b) => b.links.length - a.links.length);
  const zoneRelays = relays.filter((relay) => relay.zone_id === zone?.zone_id);

  // The side panel shows its lists, or else the latest choice in their
  // place: a key relay or link over the pair, the pair over the zone.
  const choice =
    relay !== undefined
      ? "relay"
      : openedLink !== undefined
        ? "link"
        : pair !== undefined
          ? "pair"
          : zone !== undefined
            ? "zone"
            : null;
  const listName = listTab === "relays" ? "Warnings" : "Zones";
  const zoneLabel =
    zone === undefined
      ? null
      : `${zone.zone_id}. ${names.get(zone.zone_id) ?? zone.name}`;
  const pairLabel =
    pair === undefined
      ? null
      : `${names.get(pair.zone_a)} ↔ ${names.get(pair.zone_b)}`;
  // Back goes one step, to what the choice was made from.
  const backTo =
    choice === "relay" || choice === "link"
      ? (pairLabel ?? zoneLabel ?? listName)
      : choice === "pair"
        ? (zoneLabel ?? listName)
        : listName;
  function goBack() {
    if (choice === "relay") selectRelay(null);
    else if (choice === "link") setOpenLink(null);
    else if (choice === "pair") selectPair(null);
    else selectZone(null);
  }

  // ‹ › step through the list the choice came from: a pair's links the map
  // draws, a zone's connections or key relays, or the side lists.
  const drawnPairLinks = new Set(
    drawnLinks(pair?.links ?? [], nodes).map(({ link }) => linkKey(link)),
  );
  const stepKeys =
    choice === "relay"
      ? (zone !== undefined
          ? zoneRelays
          : listTab === "relays"
            ? warningRelays
            : []
        ).map((item) => String(item.node_id))
      : choice === "link"
        ? sortLinks(pair?.links ?? [], linkSort)
            .map(linkKey)
            .filter((key) => drawnPairLinks.has(key))
        : choice === "pair"
          ? joins.map(pairKey)
          : (shown?.zones ?? []).map((item) => String(item.zone_id));
  const chosenKey =
    choice === "relay"
      ? String(selectedRelay)
      : choice === "link"
        ? openLink
        : choice === "pair"
          ? selectedPair
          : String(selectedZone);
  const stepIndex = choice === null ? -1 : stepKeys.indexOf(chosenKey ?? "");
  function stepTo(index: number) {
    const key = stepKeys[index];
    if (choice === "relay") showRelay(Number(key));
    else if (choice === "link") openPairLink(key);
    else if (choice === "pair") selectPair(key);
    else selectAndShowZone(Number(key));
  }

  // Back in a list, the row shown last of each kind is scrolled into view.
  const side = useRef<HTMLElement>(null);
  const lastRows = useRef(new Map<string, string>());
  const shownRows = [
    zone === undefined ? null : `zone-${zone.zone_id}`,
    pair === undefined ? null : `pair-${selectedPair}`,
    openedLink === undefined ? null : `link-${openLink}`,
    relay === undefined ? null : `relay-${relay.node_id}`,
  ]
    .filter((row) => row !== null)
    .join(" ");
  useEffect(() => {
    for (const row of shownRows.split(" ").filter(Boolean)) {
      lastRows.current.set(row.split("-")[0], row);
    }
    for (const row of lastRows.current.values()) {
      const element = side.current?.querySelector(`[data-row="${row}"]`);
      if (element) scrollIntoList(element);
    }
  }, [shownRows, listTab, zoneTab]);

  return (
    <section className="zones-page" aria-label="Zones">
      <div className="zones-head">
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
          <section className="zones-panel zones-settings" aria-label="Settings">
            <BuildSteps
              network={shown}
              request={request}
              open={panelOpen}
              onToggle={setPanelOpen}
              onChange={change}
            />
            <MapLegend
              network={shown}
              open={panelOpen}
              onToggle={setPanelOpen}
            />
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
              onBack={goBack}
              focus={focus}
            />
          )}
        </div>
        {shown !== null ? (
          <div className="zones-column zones-side-column">
            <aside
              ref={side}
              className="zones-panel zones-side"
              aria-label="Zones"
            >
              {choice === null ? (
                <SideLists
                  network={shown}
                  nodes={nodes}
                  colors={colors}
                  names={names}
                  warnings={warningRelays}
                  tab={listTab}
                  onTab={setListTab}
                  onSelectZone={selectAndShowZone}
                  onShowRelay={showRelay}
                />
              ) : (
                <SidebarNav
                  backTo={backTo}
                  onBack={goBack}
                  step={
                    stepIndex < 0
                      ? undefined
                      : {
                          index: stepIndex,
                          total: stepKeys.length,
                          noun: STEP_NOUNS[choice],
                          onStep: stepTo,
                        }
                  }
                />
              )}
              {relay !== undefined ? (
                <div className="zones-side-scroll">
                  <RelayCard
                    relay={relay}
                    network={shown}
                    nodes={nodes}
                    colors={colors}
                    names={names}
                    onSelectZone={selectZone}
                  />
                </div>
              ) : openedLink !== undefined ? (
                <div className="zones-side-scroll">
                  <section className="selected-node" aria-live="polite">
                    <MapLinkDetails
                      linkKind="traceroute"
                      link={{ ...openedLink, channel: shown.channel ?? "" }}
                      nodeA={linkEnd(openedLink.node_a, nodes)}
                      nodeB={linkEnd(openedLink.node_b, nodes)}
                      selectedNodeId={selectedRelay}
                      km={openedLink.km}
                      since={new Date(
                        Date.parse(shown.generated_at) -
                          shown.window_hours * HOUR_MS,
                      ).toISOString()}
                      linkSection={false}
                    />
                  </section>
                </div>
              ) : pair !== undefined ? (
                <PairSide
                  pair={pair}
                  network={shown}
                  nodes={nodes}
                  colors={colors}
                  names={names}
                  reach={reach}
                  sort={linkSort}
                  onSort={setLinkSort}
                  drawn={drawnPairLinks}
                  onOpenLink={openPairLink}
                  onSelectZone={selectZone}
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
                  tab={zoneTab}
                  onTab={setZoneTab}
                  onSelectPair={selectPair}
                  onShowRelay={showRelay}
                />
              ) : null}
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

/** A zone line as the map draws it (ZoneMap.tsx): wide, thin, or thin and dashed. */
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

/**
 * The rule for solid links, as numbered steps, each with its settings and
 * what it gave: links, then the solid ones among them. Opens and closes with
 * the legend; closed, only the sliders show.
 */
function BuildSteps({
  network,
  request,
  open,
  onToggle,
  onChange,
}: {
  network: NetworkZones;
  request: NetworkZonesRequest;
  open: boolean;
  onToggle: (open: boolean) => void;
  onChange: (next: Partial<NetworkZonesRequest>) => void;
}) {
  const changed =
    request.perDay !== DEFAULT_REQUEST.perDay ||
    request.minMarginDb !== DEFAULT_REQUEST.minMarginDb;
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
  const reset = changed ? (
    <button
      type="button"
      className="text-button"
      onClick={() =>
        onChange({
          perDay: DEFAULT_REQUEST.perDay,
          minMarginDb: DEFAULT_REQUEST.minMarginDb,
        })
      }
    >
      Reset
    </button>
  ) : null;
  return (
    <section
      className={`zones-steps${open ? "" : " is-closed"}`}
      aria-label="Which links are solid"
    >
      <details
        open={open}
        onToggle={(event) => onToggle(event.currentTarget.open)}
      >
        <summary>
          <h3>Which links are solid</h3>
        </summary>
        {/* Rendered only open, so each slider is on the page once. */}
        {open ? (
          <>
            <div className="zones-steps-heading">
              <span className="muted">
                Solid links set the lines' widths and the key relays.
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
            </ol>
          </>
        ) : null}
      </details>
      {open ? null : (
        <div className="zones-steps-sliders">
          <label>Observations {perDay}</label>
          <label>SNR margin {margin}</label>
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
      <ul className="zones-legend-items" title="Solid links between zones">
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

/**
 * Scrolls the side list holding the row just enough to show it; unlike
 * scrollIntoView, it leaves the page where it is.
 */
function scrollIntoList(row: Element) {
  const list = row.closest(".zones-side-scroll");
  if (list === null) return;
  const box = list.getBoundingClientRect();
  // A sticky table heading covers the list's top.
  const top = box.top + (list.querySelector("thead")?.offsetHeight ?? 0);
  const rowBox = row.getBoundingClientRect();
  if (rowBox.top < top) list.scrollTop -= top - rowBox.top;
  else if (rowBox.bottom > box.bottom)
    list.scrollTop += rowBox.bottom - box.bottom;
}

/** Beside the map while nothing is chosen: zones or relay warnings. */
function SideLists({
  network,
  nodes,
  colors,
  names,
  warnings,
  tab,
  onTab,
  onSelectZone,
  onShowRelay,
}: {
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  warnings: KeyRelay[];
  tab: ListTab;
  onTab: (tab: ListTab) => void;
  onSelectZone: (zoneId: number) => void;
  onShowRelay: (nodeId: number) => void;
}) {
  if (network.zones.length === 0) {
    return (
      <p className="zones-note">
        No zones: this channel has too few traceroute links in the window.
      </p>
    );
  }
  return (
    <>
      <SidebarTabs
        tabs={[
          {
            tab: "zones",
            label: "Zones",
            count: network.zones.length.toLocaleString(),
          },
          {
            tab: "relays",
            label: "Warnings",
            count: warnings.length.toLocaleString(),
          },
        ]}
        active={tab}
        onChange={onTab}
        idPrefix="zones-list"
      />
      {tab === "relays" ? (
        <RelayList
          id="zones-list-relays"
          relays={warnings}
          network={network}
          nodes={nodes}
          colors={colors}
          names={names}
          showZone
          showWarning
          onShowRelay={onShowRelay}
        />
      ) : (
        <ul
          id="zones-list-zones"
          className="zones-side-scroll zones-rows zones-zone-rows"
          aria-label="Zones"
        >
          {network.zones.map((zone) => {
            const warningCount = warnings.filter(
              (relay) => relay.zone_id === zone.zone_id,
            ).length;
            return (
              <li key={zone.zone_id}>
                <button
                  type="button"
                  data-row={`zone-${zone.zone_id}`}
                  onClick={() => onSelectZone(zone.zone_id)}
                >
                  <span className="zones-row-name">
                    <ZoneSwatch color={colors.get(zone.zone_id)} />
                    <span>
                      {zone.zone_id}. {names.get(zone.zone_id) ?? zone.name}
                    </span>
                  </span>
                  <span
                    className="zones-late"
                    title={
                      warningCount > 0
                        ? "Key relays gone quiet or low on battery"
                        : undefined
                    }
                  >
                    {warningCount > 0 ? (
                      <>
                        <WarningMark />
                        {warningCount}
                      </>
                    ) : null}
                  </span>
                  <span className="zones-row-count" title="Nodes">
                    {zone.members.length.toLocaleString()}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

/** Relays with their warning or reason for being key. */
function RelayList({
  id,
  relays,
  network,
  nodes,
  colors,
  names,
  showZone = false,
  showWarning = false,
  onShowRelay,
}: {
  id: string;
  relays: KeyRelay[];
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  /** Each relay's zone number before its name. */
  showZone?: boolean;
  showWarning?: boolean;
  onShowRelay: (nodeId: number) => void;
}) {
  if (relays.length === 0) {
    return (
      <p id={id} className="empty-inline">
        None.
      </p>
    );
  }
  const now = Date.parse(network.generated_at);
  return (
    <ul
      id={id}
      className="zones-side-scroll zones-rows"
      aria-label={showWarning ? "Warnings" : "Key relays"}
    >
      {relays.map((relay) => {
        const concern = relayConcern(relay, now);
        const onMap = placed(nodes.get(relay.node_id)) !== null;
        return (
          <li key={relay.node_id}>
            <button
              type="button"
              data-row={`relay-${relay.node_id}`}
              className={onMap ? undefined : "unplaced"}
              title={onMap ? undefined : "Not on the map: no position"}
              onClick={() => onShowRelay(relay.node_id)}
            >
              <span className="zones-row-name">
                {showZone && relay.zone_id !== null ? (
                  <ZoneNumber zoneId={relay.zone_id} colors={colors} />
                ) : null}
                <span>
                  {nodeName(nodes.get(relay.node_id)) ??
                    formatNodeId(relay.node_id)}
                </span>
              </span>
              <span
                className={`zones-value${concern === null ? "" : " zones-late"}`}
                title={showWarning ? undefined : (concern ?? undefined)}
              >
                {concern === null ? null : "⚠ "}
                {showWarning
                  ? concern
                  : relayReason(relay, names, network.rules)}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * A chosen key relay: what it matters for, the nodes that depend on it, and
 * how it is doing, with the Map page and its row in the table.
 */
function RelayCard({
  relay,
  network,
  nodes,
  colors,
  names,
  onSelectZone,
}: {
  relay: KeyRelay;
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  onSelectZone: (zoneId: number) => void;
}) {
  const nodeId = relay.node_id;
  const node = nodes.get(nodeId);
  const now = Date.parse(network.generated_at);
  const concern = relayConcern(relay, now);
  const hours = hoursSince(relay.last_seen_at, now);
  const nodeCount = (count: number) => plural(count, "node");
  return (
    <section className="selected-node" aria-live="polite">
      <div className="selected-node-heading">
        <span className="node-avatar avatar-green" title="Short name">
          {shortLabel(nodeId, node?.short_name ?? null)}
        </span>
        <div>
          <h3>
            {holdsOnlySolidLink(relay) ? (
              <RelayMark filled />
            ) : isOutlined(relay, network.rules) ? (
              <RelayMark filled={false} />
            ) : null}{" "}
            {nodeName(node) ?? formatNodeId(nodeId)}
          </h3>
          <span className="muted">
            <CopyValue value={formatNodeId(nodeId)} label="node ID" />
          </span>
        </div>
        <a
          className="icon-button"
          href={nodeHref(nodeId)}
          aria-label="View node details"
          title="View node details"
        >
          <Icon name="open" />
        </a>
      </div>
      <p className="zones-relay-key">
        {concern !== null ? <span className="zones-late">⚠ </span> : null}
        Key for: {relayReason(relay, names, network.rules)}
        {concern !== null ? `. ${concern}.` : null}
      </p>
      <dl className="node-summary node-card-facts">
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
        <DataItem label="Role" value={formatRole(node?.role ?? null)} />
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
        {node?.estimated ? (
          <DataItem label="Position" value="Estimated from its neighbours" />
        ) : null}
      </dl>
      <div className="zones-relay-actions">
        {placed(node) !== null ? (
          <a className="secondary-button" href={mapHref(nodeId)}>
            <Icon name="pin" /> On the Map page
          </a>
        ) : null}
        <button
          type="button"
          className="secondary-button"
          onClick={() => showRelayRow(nodeId)}
        >
          <Icon name="table" /> Row in the table
        </button>
      </div>
    </section>
  );
}

/** The id of a key relay's row in the table below the map. */
function relayRowId(nodeId: number): string {
  return `key-relay-${nodeId}`;
}

/**
 * Scrolls the page to a key relay's row in the table and flashes it. The
 * chosen relay always has a row (KeyRelays keeps it).
 */
function showRelayRow(nodeId: number) {
  const row = document.getElementById(relayRowId(nodeId));
  if (row === null) return;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.querySelector("a")?.focus({ preventScroll: true });
  // Restart the flash on a second click.
  row.classList.remove("is-flashing");
  void row.offsetWidth;
  row.classList.add("is-flashing");
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
 * (between reported positions only, with line of sight). The side panel,
 * too narrow for it, leaves out SNR; an open link shows it.
 */
function LinkMeasurementCells({
  link,
  nodes,
  snr = true,
}: {
  link: ZonePairLink;
  nodes: NodeIndex;
  snr?: boolean;
}) {
  return (
    <>
      <td className="number">
        <BothWays
          left={link.a_to_b.observations.toLocaleString()}
          right={link.b_to_a.observations.toLocaleString()}
        />
      </td>
      {snr ? (
        <td className="number">
          <LinkSnr link={link} nodes={nodes} />
        </td>
      ) : null}
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
 * A zone as a row like a selected link's node: its number on its colour, its
 * name, and its size and region under it. With onSelect, the row selects the
 * zone.
 */
function ZoneEnd({
  zone,
  colors,
  names,
  onSelect,
}: {
  zone: Zone;
  colors: Map<number, string>;
  names: Map<number, string>;
  onSelect?: (zoneId: number) => void;
}) {
  const name = names.get(zone.zone_id) ?? zone.name;
  const content = (
    <>
      <span
        className="node-avatar zones-zone-badge"
        style={{ background: colors.get(zone.zone_id) }}
        aria-hidden="true"
      >
        {zone.zone_id}
      </span>
      <span className="link-end-names">
        <span className="link-end-name">{name}</span>
        <span className="link-end-id">
          {[
            plural(zone.members.length, "node"),
            zone.place !== null ? zone.region : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
      </span>
    </>
  );
  if (onSelect === undefined) {
    return <div className="zones-zone-end">{content}</div>;
  }
  return (
    <button
      type="button"
      className="link-end zones-zone-end"
      aria-label={`${zone.zone_id}. ${name}`}
      title={name}
      onClick={() => onSelect(zone.zone_id)}
    >
      {content}
      <Icon name="chevron" />
    </button>
  );
}

/**
 * The chosen pair: its two zones joined by a line with how far apart they
 * are, its figures, then its links compact for the narrow column (zone
 * number headings and short names), in the order the pair's links table
 * under its row uses too.
 */
function PairSide({
  pair,
  network,
  nodes,
  colors,
  names,
  reach,
  sort,
  onSort,
  drawn,
  onOpenLink,
  onSelectZone,
}: {
  pair: ZonePair;
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  reach: ReachLookup;
  sort: LinkSort;
  onSort: (sort: LinkSort) => void;
  /** The pair's links the map draws, which open there. */
  drawn: ReadonlySet<string>;
  onOpenLink: (link: string) => void;
  onSelectZone: (zoneId: number) => void;
}) {
  const unplaced = pair.links.length - drawn.size;
  const solid = pair.links.filter((link) => link.solid).length;
  const texts = [
    reach(pair.zone_a, pair.zone_b),
    reach(pair.zone_b, pair.zone_a),
  ] as const;
  const links = sortLinks(pair.links, sort);
  const end = (zoneId: number) => {
    const zone = network.zones.find((item) => item.zone_id === zoneId);
    return zone === undefined ? null : (
      <ZoneEnd
        zone={zone}
        colors={colors}
        names={names}
        onSelect={onSelectZone}
      />
    );
  };
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
      <div className="link-ends zones-pair-ends">
        {end(pair.zone_a)}
        <div className="link-ends-join">
          <span className="link-ends-line" aria-hidden="true" />
          <span className="link-ends-km">
            {pair.km !== null ? (
              <strong title="Between the zone centres">
                {formatKm(pair.km)} km
              </strong>
            ) : (
              <span className="muted">Distance unknown</span>
            )}
            <span title="The fewest hops between the two zone hubs">
              {pair.hub_hops !== null
                ? `${plural(pair.hub_hops, "hop")} between hubs`
                : "No path between hubs"}
            </span>
          </span>
        </div>
        {end(pair.zone_b)}
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
      <div className="zones-side-scroll zones-pair-scroll">
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
              <SortHeading
                label="Margin"
                title="Weaker direction's signal margin"
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
                  data-row={`link-${key}`}
                  className={placed ? "zones-pair-row" : "zones-unplaced-row"}
                  title={
                    placed
                      ? undefined
                      : "Not on the map: a node has no position"
                  }
                  onClick={
                    placed
                      ? (event) => {
                          // Node and line of sight links keep their own behaviour.
                          if ((event.target as Element).closest("a")) return;
                          onOpenLink(key);
                        }
                      : undefined
                  }
                >
                  <td>{node(link.node_a)}</td>
                  <td>{node(link.node_b)}</td>
                  <LinkMeasurementCells link={link} nodes={nodes} snr={false} />
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
      </div>
    </>
  );
}

/**
 * A chosen zone: its facts, then the zones it joins (most links first; a row
 * shows the pair) or its key relays.
 */
function ZoneDetails({
  zone,
  joins,
  relays,
  network,
  nodes,
  colors,
  names,
  tab,
  onTab,
  onSelectPair,
  onShowRelay,
}: {
  zone: Zone;
  joins: ZonePair[];
  relays: KeyRelay[];
  network: NetworkZones;
  nodes: NodeIndex;
  colors: Map<number, string>;
  names: Map<number, string>;
  tab: ZoneTab;
  onTab: (tab: ZoneTab) => void;
  onSelectPair: (pair: string) => void;
  onShowRelay: (nodeId: number) => void;
}) {
  const placed = zone.members.filter(
    (id) => nodes.get(id)?.latitude != null,
  ).length;
  // The zones it can reach (its pairs), and those its solid paths on the map
  // reach.
  const onPaths = new Set(zone.solid_paths.flat());
  const paired = new Set(
    network.zone_pairs.flatMap((pair) =>
      pair.zone_a === zone.zone_id
        ? [pair.zone_b]
        : pair.zone_b === zone.zone_id
          ? [pair.zone_a]
          : [],
    ),
  );
  const others = network.zones.filter((other) => paired.has(other.zone_id));
  const reached = others.filter((other) =>
    other.members.some((id) => onPaths.has(id)),
  ).length;
  return (
    <>
      <ZoneEnd zone={zone} colors={colors} names={names} />
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
      <SidebarTabs
        tabs={[
          {
            tab: "connections",
            label: "Connections",
            count: joins.length.toLocaleString(),
          },
          {
            tab: "relays",
            label: "Key relays",
            count: relays.length.toLocaleString(),
          },
        ]}
        active={tab}
        onChange={onTab}
        idPrefix="zone-list"
      />
      {tab === "relays" ? (
        <RelayList
          id="zone-list-relays"
          relays={relays}
          network={network}
          nodes={nodes}
          colors={colors}
          names={names}
          onShowRelay={onShowRelay}
        />
      ) : joins.length === 0 ? (
        <p id="zone-list-connections" className="empty-inline">
          None.
        </p>
      ) : (
        <ul
          id="zone-list-connections"
          className="zones-side-scroll zones-rows"
          aria-label="Connections"
        >
          {joins.map((pair) => {
            const other =
              pair.zone_a === zone.zone_id ? pair.zone_b : pair.zone_a;
            const solid = pair.links.filter((link) => link.solid).length;
            const warning = pair.warnings.find((item) =>
              SOLID_WARNINGS.includes(item),
            );
            return (
              <li key={pairKey(pair)}>
                <button
                  type="button"
                  data-row={`pair-${pairKey(pair)}`}
                  title={`Best link's signal margin${warning === undefined ? "" : `. ${warningTitle(warning, pair, nodes) ?? warning}`}`}
                  onClick={() => onSelectPair(pairKey(pair))}
                >
                  <span className="zones-row-name">
                    <TierDot
                      color={linkTierColor(
                        marginTier(pair.strongest_margin_db),
                      )}
                    />
                    <ZoneNumber zoneId={other} colors={colors} />
                    <span>{names.get(other)}</span>
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
    </>
  );
}

// ------------------------------------------------------------ Between zones

const PAIR_SORTS = [
  "zones",
  "links",
  "texts",
  "observations",
  "margin",
  "hub_hops",
  "way_round",
  "km",
  "solid",
] as const;
type PairSort = (typeof PAIR_SORTS)[number];

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
  }
}

/**
 * Zone pairs in the chosen order; ties keep the order zones.py lists them in,
 * by zone.
 */
function sortPairs(
  pairs: ZonePair[],
  order: PairOrder,
  reach: ReachLookup,
): ZonePair[] {
  const sign = order.descending ? -1 : 1;
  return [...pairs].sort((a, b) => {
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
    (value) => PAIR_SORTS.includes((value as PairOrder | null)?.by as PairSort),
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
                      <td className="number">
                        {direct
                          ? pair.links.filter((link) => link.solid).length
                          : none}
                      </td>
                      <td
                        className="number"
                        title={
                          direct ? undefined : "Joined only through other zones"
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
                        <td colSpan={10}>
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

/** What a pair warning means, for its tooltip. */
function warningTitle(
  warning: PairWarning,
  pair: ZonePair,
  nodes: NodeIndex,
): string | undefined {
  if (warning === "no solid route") {
    return "No solid path joins these zones, even through others";
  }
  if (warning === "one solid link") {
    return "If either end goes quiet, the zones lose their solid link";
  }
  if (warning === "one relay") {
    const relay = pair.sole_relay;
    const solid = pair.links.filter((link) => link.solid).length;
    const name = relay === null ? "one node" : directionEnd(relay, nodes);
    return `All ${solid} solid links pass through ${name}`;
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
            title="Weaker direction's signal margin"
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
  // The chosen relay stays in the table whatever the zone and rows, first
  // when it would be hidden.
  const shownRows = showAll ? sorted : sorted.slice(0, RELAY_ROWS);
  const picked = network.key_relays.find(
    (relay) => relay.node_id === selectedRelay,
  );
  const rows =
    picked === undefined || shownRows.includes(picked)
      ? shownRows
      : [picked, ...shownRows];
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
                    id={relayRowId(relay.node_id)}
                    className={
                      relay.node_id === selectedRelay
                        ? "is-selected"
                        : undefined
                    }
                    onAnimationEnd={(event) =>
                      event.currentTarget.classList.remove("is-flashing")
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
