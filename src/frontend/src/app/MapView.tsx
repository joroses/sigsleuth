import {
  Fragment,
  useCallback,
  useDeferredValue,
  useEffect,
  useEffectEvent,
  useMemo,
  useRef,
  useState,
} from "react";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import {
  type CoverageCellDetail,
  type CoverageCellList,
  type CoverageExtent,
  type CoverageNodeList,
  type CoverageViewNode,
  type LinkKind,
  MAP_LINK_LIMIT,
  MAP_NODE_LIMIT,
  MapLink,
  MapLinkList,
  MapNode,
  MapNodeList,
  type NodeOverview,
  nodeUrl,
  type TextReach,
  type TextSender,
  type TextSenderList,
  type ZeroCostHop,
  fetchMapNode,
  textReachUrl,
} from "./api";
import { BASEMAPS, findBasemap } from "./basemaps";
import { CoverageCellDetails } from "./CoverageCellDetails";
import {
  COVERAGE_FILL_LAYER,
  COVERAGE_SOURCES,
  COVERAGE_SOURCE,
  COVERAGE_LINES_SOURCE,
  addCoverageLayers,
  buildCoverageFeatureCollection,
  buildCoverageLines,
  cellBounds,
  cellText,
  coverageCellUrl,
  coverageCellsUrl,
  coverageExtentUrl,
  coverageNodesUrl,
  cellsInView,
  nextCoverageArea,
  viewSpreadingFactor,
  type CellAddress,
  type CoverageFeatureCollection,
  type CoverageLineCollection,
  type CoverageArea,
  type CoverageView,
} from "./coverageLayer";
import { HeardInView } from "./HeardInView";
import { MapFilterPanel } from "./MapFilterPanel";
import { MapLinkDetails } from "./MapLinkDetails";
import { bothWaysObservations } from "./linkQuality";
import { fromNode, weakerMargin } from "./linkRows";
import {
  LINK_KINDS,
  LINK_LAYERS,
  LINK_MIN_ZOOM,
  addLinkLayers,
  buildLinkFeatureCollection,
  DEFAULT_LAYER_CHOICE,
  DEFAULT_MIN_OBSERVATIONS,
  drawnKm,
  drawnLongEnough,
  linkKey,
  linkSourceId,
  showLinkFocus,
  linkTierShown,
  ALL_LINK_TIERS,
  MAX_LINK_KM,
  withinHops,
  ZERO_COST_LAYER,
  addZeroCostLayer,
  zeroCostArrows,
  type LinkFeatureCollection,
  type LinkTierChoice,
  type ZeroCostArrows,
} from "./mapLinkLayers";
import {
  NODE_LAYERS,
  addNodeLayers,
  clickableNodeLayers,
  applyNodeView,
  buildNodeFeatureCollection,
  type NodeFeatureCollection,
  type NodeView,
  type TextShare,
} from "./mapNodeLayers";
import { spiderElement } from "./mapNodeSpider";
import { ChattyNodes } from "./ChattyNodes";
import { nearestLine } from "./lineHit";
import { isNarrowScreen } from "./narrowScreen";
import {
  formatHardware,
  formatKm,
  formatNodeId,
  formatRole,
  nodeHref,
  nodeLabel,
  nodeMarkerLabel,
  positionSource,
  shortLabel,
} from "./nodeDisplay";
import {
  ANY,
  busiestChannel,
  DEFAULT_FILTER,
  filterCounts,
  linkableNodes,
  NOT_REPORTED,
  onChosenChannel,
  shownNodes,
} from "./mapNodeFilter";
import {
  DEFAULT_SEEN_WINDOW,
  isSeenWindow,
  mapUrls,
  mapWindowStart,
  SHOWN_WINDOW_VIEW,
  type SeenWindow,
  type ShownWindow,
} from "./mapTimeFilter";
import { NodeSearch } from "./NodeSearch";
import {
  loadSetting,
  readView,
  saveSetting,
  useLinkedSetting,
  useSetting,
  useViewState,
  writeView,
} from "./remember";
import { type MapLinkView, mapViewHref } from "./routes";
import { useApi } from "./useApi";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";
import { formatHours } from "./zones";
import {
  BothWays,
  CopyValue,
  Icon,
  IconButton,
  MarginValue,
  ShareButton,
  ShortTime,
  SidebarNav,
  SidebarTabs,
  StatusPanel,
} from "./ui";

const NO_NODES: MapNode[] = [];
const NO_LINKS: MapLink[] = [];
const NO_SHARES: TextShare[] = [];

declare global {
  interface Window {
    sigsleuthMap?: maplibregl.Map;
  }
}

// Half the side of the square kept in view around a chosen node: about 300 × 300 km.
const FOCUS_HALF_SIDE_KM = 150;
const KM_PER_DEGREE_LATITUDE = 111.32;

// On a phone the sidebar sheet covers up to 55% of the map's bottom (see
// styles.css), so a chosen place is centred in the part above it.
function sheetOffset(instance: maplibregl.Map): [number, number] {
  if (!isNarrowScreen()) return [0, 0];
  return [0, -instance.getContainer().clientHeight * 0.275];
}

// Room around places fitted in view: a margin, and on a phone the sheet's
// height, split above and below since sheetOffset then lifts them above it.
function fitPadding(instance: maplibregl.Map): maplibregl.PaddingOptions {
  const half = -sheetOffset(instance)[1];
  return { top: 60 + half, bottom: 60 + half, left: 60, right: 60 };
}

function moveCamera(
  instance: maplibregl.Map,
  options: maplibregl.FlyToOptions,
) {
  // flyTo drops the sheet offset when reduced motion is enabled.
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches)
    instance.easeTo({ ...options, duration: 0 });
  else instance.flyTo(options);
}

// Centre an unconnected node with about 300 km around it.
function focusNode(instance: maplibregl.Map, node: MapNode) {
  const { latitude, longitude } = node;
  const latitudeDelta = FOCUS_HALF_SIDE_KM / KM_PER_DEGREE_LATITUDE;
  // Clamped so the box stays finite near the poles.
  const longitudeDelta =
    latitudeDelta / Math.max(Math.cos((latitude * Math.PI) / 180), 0.05);
  const camera = instance.cameraForBounds([
    [longitude - longitudeDelta, latitude - latitudeDelta],
    [longitude + longitudeDelta, latitude + latitudeDelta],
  ]);
  moveCamera(instance, {
    center: [longitude, latitude],
    zoom: Math.max(camera?.zoom ?? 0, instance.getZoom()),
    offset: sheetOffset(instance),
  });
}

// Zoom out, if either node is off screen, until both are in view, but not below
// the zoom where links are drawn; a very long link may still be cut off.
function focusLink(instance: maplibregl.Map, a: MapNode, b: MapNode) {
  const ends: [number, number][] = [a, b].map((node) => [
    node.longitude,
    node.latitude,
  ]);
  const bounds = instance.getBounds();
  if (ends.every((end) => bounds.contains(end))) return;
  const camera = instance.cameraForBounds(
    new maplibregl.LngLatBounds(ends[0], ends[0]).extend(ends[1]),
    { padding: fitPadding(instance) },
  );
  if (camera === undefined) return;
  moveCamera(instance, {
    center: camera.center,
    zoom: Math.min(
      Math.max(camera.zoom ?? 0, LINK_MIN_ZOOM),
      instance.getZoom(),
    ),
    offset: sheetOffset(instance),
  });
}

// Fit a sender and the gateways that reported its texts, but not below the zoom
// where clusters split, as nodes inside a cluster stay hidden during a selection.
const TEXT_REACH_MIN_ZOOM = 5;

// A sender without a position (null) is fitted to its gateways alone.
function focusTextReach(
  instance: maplibregl.Map,
  sender: MapNode | null,
  gateways: MapNode[],
) {
  if (gateways.length === 0) {
    if (sender !== null) focusNode(instance, sender);
    return;
  }
  const bounds = new maplibregl.LngLatBounds();
  for (const node of sender === null ? gateways : [sender, ...gateways])
    bounds.extend([node.longitude, node.latitude]);
  const camera = instance.cameraForBounds(bounds, {
    padding: fitPadding(instance),
  });
  if (camera === undefined) return;
  moveCamera(instance, {
    center: camera.center,
    zoom: Math.max(camera.zoom ?? 0, TEXT_REACH_MIN_ZOOM),
    offset: sheetOffset(instance),
  });
}

// Show a node, its coverage and other nodes: only when some are out of
// view, and without zooming in, unless zoomIn fits them whatever the view.
function focusCoverage(
  instance: maplibregl.Map,
  node: MapNode | null,
  extent: CoverageExtent | null,
  zoomIn: boolean,
  otherNodes: MapNode[] = NO_NODES,
  cell?: CellAddress,
) {
  if (extent === null && cell === undefined) {
    focusTextReach(instance, node, otherNodes);
    return;
  }
  const bounds = new maplibregl.LngLatBounds();
  if (extent !== null) {
    bounds.extend([extent.west, extent.south]);
    bounds.extend([extent.east, extent.north]);
  }
  if (cell !== undefined) {
    const [west, south, east, north] = cellBounds(cell);
    bounds.extend([west, south]);
    bounds.extend([east, north]);
  }
  if (node !== null) bounds.extend([node.longitude, node.latitude]);
  for (const other of otherNodes)
    bounds.extend([other.longitude, other.latitude]);
  const view = instance.getBounds();
  if (
    !zoomIn &&
    view.contains(bounds.getSouthWest()) &&
    view.contains(bounds.getNorthEast())
  )
    return;
  const camera = instance.cameraForBounds(bounds, {
    padding: fitPadding(instance),
  });
  if (camera === undefined) return;
  moveCamera(instance, {
    center: camera.center,
    zoom: Math.min(
      Math.max(camera.zoom ?? 0, otherNodes.length > 0 ? LINK_MIN_ZOOM : 0),
      zoomIn ? 14 : instance.getZoom(),
    ),
    offset: sheetOffset(instance),
  });
}

const LINK_NAMES: Record<LinkKind, string> = {
  traceroute: "traceroute links",
  direct: "direct links",
  neighbor: "neighbour links",
};

interface SelectedLink {
  linkKind: LinkKind;
  link: MapLink;
}

const CAMERA_KEY = "map.camera";
const SELECTED_VIEW_KEY = "map.selected";

interface Camera {
  center: [number, number];
  zoom: number;
}

/** Where this history entry, or else the last visit, left the map. */
function savedCamera(): Camera | null {
  for (const saved of [readView(CAMERA_KEY), loadSetting(CAMERA_KEY)]) {
    const camera = saved as Partial<Camera> | null | undefined;
    if (
      Array.isArray(camera?.center) &&
      camera.center.length === 2 &&
      camera.center.every(Number.isFinite) &&
      Number.isFinite(camera.zoom)
    )
      return { center: camera.center, zoom: camera.zoom as number };
  }
  return null;
}

/** A new selection fits again and drops any pending fit. */
interface Selection {
  nodeId: number;
  /** "view" fits all enabled layers together. */
  fit: "none" | "coverage" | "view";
  /** A node from either sidebar list with no recent position. */
  unplaced?: TextSender | CoverageViewNode;
  /** Fit coverage even when it is already in view, allowing a closer zoom. */
  zoomIn?: boolean;
  /** Keep the source cell in view even if a relay hint has no coverage. */
  coverageCell?: CellAddress;
}

/**
 * One kind of link, fetched only while url is set (and kept for Back under
 * chosenAt, see useApi), and its map features. Links observed fewer than
 * minObservations times, in both directions together, touching a node the
 * category, role or estimate filter hides (whatever its latest channel), on
 * another channel than the one chosen, with neither half of a chosen quality
 * tier, or drawn shorter than minKm, are left out everywhere: drawn and walked
 * for hop depth (see linkTierShown and drawnLongEnough).
 */
function useLinkLayer(
  linkKind: LinkKind,
  url: string | null,
  chosenAt: number,
  nodeMap: ReadonlyMap<number, MapNode>,
  minObservations: number,
  filteredOut: ReadonlySet<number>,
  channel: string,
  tiers: LinkTierChoice,
  minKm: number,
) {
  const { data: page, error, loading } = useApi<MapLinkList>(url, chosenAt);
  const loaded = page?.items ?? NO_LINKS;
  const allTiers = Object.values(tiers).every(Boolean);
  const links = useMemo(
    () =>
      minObservations <= 1 &&
      filteredOut.size === 0 &&
      channel === ANY &&
      allTiers &&
      minKm <= 0
        ? loaded
        : loaded.filter(
            (link) =>
              link.a_to_b.observations + link.b_to_a.observations >=
                minObservations &&
              !filteredOut.has(link.node_a) &&
              !filteredOut.has(link.node_b) &&
              onChosenChannel(link, channel) &&
              linkTierShown(tiers, link) &&
              drawnLongEnough(link, nodeMap, minKm),
          ),
    [
      loaded,
      minObservations,
      filteredOut,
      channel,
      tiers,
      allTiers,
      minKm,
      nodeMap,
    ],
  );
  const data = useMemo(
    () => buildLinkFeatureCollection(linkKind, links, nodeMap),
    [linkKind, links, nodeMap],
  );
  const zeroCost = page?.zero_cost_hops ?? NO_ZERO_COST;
  const arrows = useMemo(
    () => zeroCostArrows(links, zeroCost, nodeMap),
    [links, zeroCost, nodeMap],
  );
  return { page, error, loading, links, data, arrows };
}

const NO_ZERO_COST: ZeroCostHop[] = [];

// "idle" follows the source update and rendered layers; scripts/perf-map.sh reads it.
function measureNodeLayersIdle(instance: maplibregl.Map) {
  instance.once("idle", () => performance.measure("map: node layers idle"));
}

/**
 * nodeId, from #map/<node number>, is selected once nodes load; `linkView`,
 * from a shared link's query, says what the map shows of it (see MapLinkView).
 */
export function MapView({
  nodeId = null,
  linkView = null,
}: {
  nodeId?: number | null;
  linkView?: MapLinkView | null;
}) {
  const map = useRef<maplibregl.Map | null>(null);
  const nodesById = useRef(new Map<number, MapNode>());
  // The node layers' latest data, for the next style load.
  const nodeView = useRef<{
    data: NodeFeatureCollection;
    focusActive: boolean;
  }>({ data: buildNodeFeatureCollection([]), focusActive: false });
  const pendingLinkData = useRef<Record<LinkKind, LinkFeatureCollection>>({
    traceroute: buildLinkFeatureCollection("traceroute", [], new Map()),
    direct: buildLinkFeatureCollection("direct", [], new Map()),
    neighbor: buildLinkFeatureCollection("neighbor", [], new Map()),
  });
  const pendingArrows = useRef<ZeroCostArrows>(
    zeroCostArrows([], [], new Map()),
  );
  const linksByKey = useRef(new Map<string, SelectedLink>());
  const selectedLinkKey = useRef<string | null>(null);
  const cellChosen = useRef(false);
  // Clears the hovered cell's highlight and popup, set by setUpMap.
  const forgetCellHover = useRef<() => void>(() => {});
  // The only links shown; null shows them all.
  const linkFocus = useRef<string[] | null>(null);
  const [seenWindow, setSeenWindow] = useSetting<SeenWindow>(
    "seenWindow",
    DEFAULT_SEEN_WINDOW,
    isSeenWindow,
  );
  // The window's start is fixed when it is chosen, or reused from minutes ago
  // (mapWindowStart). It stays in this history entry, so Back shows what was
  // loaded for it.
  // startMapRequests may have kept it already.
  const [shownWindow, setShownWindow] = useViewState<ShownWindow>(
    SHOWN_WINDOW_VIEW,
    () => mapWindowStart(linkView?.window ?? seenWindow),
  );
  const { chosenAt } = shownWindow;
  const urls = useMemo(
    () => mapUrls(shownWindow.window, chosenAt),
    [shownWindow.window, chosenAt],
  );
  // Every recent position loads once per visit; MapLibre culls what is off screen.
  const {
    data: page,
    error,
    loading,
  } = useApi<MapNodeList>(urls.nodes, chosenAt);
  // The node named by #map/<node number> when the window leaves it out.
  const [linkedNode, setLinkedNode] = useState<MapNode | null>(null);
  const loadedNodes = useMemo(() => {
    const nodes = page?.items ?? NO_NODES;
    return linkedNode === null ||
      nodes.some((node) => node.node_id === linkedNode.node_id)
      ? nodes
      : [...nodes, linkedNode];
  }, [page, linkedNode]);
  // Category, role and channel filters apply in the browser to the loaded nodes,
  // except the node #map/<node number> asked for, until another is chosen.
  // Links end only at nodes the filters other than channel keep (see
  // linkableNodes); the nodes drawn follow the links shown (see shownNodes).
  // Read before the filter is first kept, which would hide a first visit.
  const [firstVisit] = useState(() => loadSetting("nodeFilter") === undefined);
  const [nodeFilter, setNodeFilter] = useLinkedSetting(
    "nodeFilter",
    DEFAULT_FILTER,
    linkView === null
      ? undefined
      : { ...DEFAULT_FILTER, channel: linkView.channel ?? ANY },
  );
  // A first visit starts on the busiest channel once the nodes load, unless
  // a shared link chose the channel.
  const [channelPending, setChannelPending] = useState(
    firstVisit && linkView === null,
  );
  if (channelPending && page !== null) {
    setChannelPending(false);
    const channel = busiestChannel(page.items);
    if (channel !== null) setNodeFilter({ ...nodeFilter, channel });
  }
  const [requestedId, setRequestedId] = useState(nodeId);
  const linkable = useMemo(
    () => linkableNodes(loadedNodes, nodeFilter, requestedId),
    [loadedNodes, nodeFilter, requestedId],
  );
  const filteredOut = useMemo(() => {
    const kept = new Set(linkable.map((node) => node.node_id));
    return new Set(
      loadedNodes
        .map((node) => node.node_id)
        .filter((nodeId) => !kept.has(nodeId)),
    );
  }, [loadedNodes, linkable]);
  const linkableMap = useMemo(
    () => new Map(linkable.map((node) => [node.node_id, node])),
    [linkable],
  );
  const counts = useMemo(
    () => filterCounts(loadedNodes, nodeFilter),
    [loadedNodes, nodeFilter],
  );
  const [linkChoice, setLinkChoice] = useLinkedSetting(
    "linkLayers",
    DEFAULT_LAYER_CHOICE,
    linkView === null
      ? undefined
      : {
          traceroute: linkView.show.includes("traceroute"),
          direct: linkView.show.includes("direct"),
          neighbor: linkView.show.includes("neighbor"),
        },
  );
  const [linkTiers, setLinkTiers] = useSetting("linkTiers", ALL_LINK_TIERS);
  const [showTextReach, setShowTextReach] = useLinkedSetting(
    "textReach",
    true,
    linkView?.show.includes("reach"),
  );
  const [showCoverage, setShowCoverage] = useLinkedSetting(
    "coverage",
    false,
    linkView?.show.includes("coverage"),
  );
  // The map's view and the area cells are fetched for, set when the map stops.
  const [coverageArea, setCoverageArea] = useState<CoverageArea | null>(null);
  const [selectedCell, setSelectedCell] = useState<CellAddress | null>(null);
  const [unknownCoverage, setUnknownCoverage] = useState(false);
  // The coverage layers' latest data, for the next style load.
  const coverageData = useRef<{
    cells: CoverageFeatureCollection;
    lines: CoverageLineCollection;
  }>({
    cells: buildCoverageFeatureCollection(null),
    lines: buildCoverageLines(null, [], new Map()),
  });
  // Kept as typed, so the box can be emptied; anything below 1 means 1.
  const [minObservationsText, setMinObservationsText] = useSetting(
    "minObservations",
    DEFAULT_MIN_OBSERVATIONS,
  );
  const minObservations = Math.max(
    1,
    Math.floor(Number(minObservationsText)) || 1,
  );
  const [minKm, setMinKm] = useSetting(
    "minLinkKm",
    0,
    (value) => typeof value === "number" && value >= 0 && value <= MAX_LINK_KM,
  );
  // Dragging the slider redraws links and nodes; deferring lets the slider
  // keep up and skips lengths it has already passed.
  const drawnMinKm = useDeferredValue(minKm);
  // Links are optional; a failure leaves the nodes on the map.
  const tracerouteLayer = useLinkLayer(
    "traceroute",
    linkChoice.traceroute ? urls.traceroute : null,
    chosenAt,
    linkableMap,
    minObservations,
    filteredOut,
    nodeFilter.channel,
    linkTiers,
    drawnMinKm,
  );
  const directLayer = useLinkLayer(
    "direct",
    linkChoice.direct ? urls.direct : null,
    chosenAt,
    linkableMap,
    minObservations,
    filteredOut,
    nodeFilter.channel,
    linkTiers,
    drawnMinKm,
  );
  const neighborLayer = useLinkLayer(
    "neighbor",
    linkChoice.neighbor ? urls.neighbor : null,
    chosenAt,
    linkableMap,
    minObservations,
    filteredOut,
    nodeFilter.channel,
    linkTiers,
    drawnMinKm,
  );
  const linkLayers = {
    traceroute: tracerouteLayer,
    direct: directLayer,
    neighbor: neighborLayer,
  };
  const tracerouteLinks = tracerouteLayer.links;
  const directLinks = directLayer.links;
  const neighborLinks = neighborLayer.links;
  // Only links with both ends placed are drawn, so only they bring their nodes.
  const linkEnds = useMemo(() => {
    const ends = new Set<number>();
    for (const links of [tracerouteLinks, directLinks, neighborLinks])
      for (const link of links)
        if (linkableMap.has(link.node_a) && linkableMap.has(link.node_b))
          ends.add(link.node_a).add(link.node_b);
    return ends;
  }, [tracerouteLinks, directLinks, neighborLinks, linkableMap]);
  const items = useMemo(
    () => shownNodes(loadedNodes, nodeFilter, requestedId, linkEnds),
    [loadedNodes, nodeFilter, requestedId, linkEnds],
  );
  const nodeMap = useMemo(
    () => new Map(items.map((node) => [node.node_id, node])),
    [items],
  );
  const [selection, setSelection] = useState<Selection | null>(() =>
    nodeId === null ? null : { nodeId, fit: "view", zoomIn: true },
  );
  // The chosen link's key; it shows only while the filters keep the link.
  const [chosenLinkKey, setChosenLinkKey] = useState<string | null>(null);
  // A selected node the filters hide shows as nothing selected, and comes
  // back when they no longer hide it.
  const selectedNode =
    selection === null ? null : (nodeMap.get(selection.nodeId) ?? null);
  const unplaced = selection?.unplaced ?? null;
  const unplacedNode =
    selectedNode === null &&
    unplaced !== null &&
    (!("last_sent_at" in unplaced) ||
      urls.since === undefined ||
      Date.parse(unplaced.last_sent_at) >= Date.parse(urls.since))
      ? unplaced
      : null;
  const nodeDetails = useApi<NodeOverview>(
    unplacedNode === null ? null : nodeUrl(unplacedNode.node_id),
  );
  const detailNode = selectedNode ?? nodeDetails.data?.node ?? null;
  const [hopDepth, setHopDepth] = useViewState("map.hopDepth", 0);
  // On a phone the sidebar is a sheet over the map: closed at first, unless
  // a link chose a node, and opened by choosing something on the map. The
  // camera leaves room for it either way (sheetOffset).
  const [collapsed, setCollapsed] = useState(
    () => isNarrowScreen() && nodeId === null,
  );
  // Phones start with the filters closed so the map is visible. There the
  // filters and the sheet never show together: opening one closes the other.
  const [filtersOpen, setFiltersOpen] = useSetting(
    "filterPanelOpen",
    !isNarrowScreen(),
  );
  if (filtersOpen && !collapsed && isNarrowScreen()) setFiltersOpen(false);
  const [listTab, setListTab] = useSetting<ListTab>(
    "map.listTab",
    "chatty",
    (value) => value === "chatty" || value === "heard",
  );
  const [mapMessage, setMapMessage] = useState<string | null>(null);
  const [basemap, setBasemap] = useSetting("basemap", BASEMAPS[0].id, (value) =>
    BASEMAPS.some((option) => option.id === value),
  );
  const shownBasemap = useRef(basemap);

  // The only ways the node selection changes. The sidebar shows the latest
  // choice, so a node replaces a chosen link or cell.
  const select = useCallback((node: MapNode) => {
    setUnknownCoverage(false);
    setSelection({ nodeId: node.node_id, fit: "view", zoomIn: true });
    setChosenLinkKey(null);
    setSelectedCell(null);
  }, []);
  const clearSelection = useCallback(() => {
    setUnknownCoverage(false);
    setSelection(null);
    setChosenLinkKey(null);
  }, []);

  useEffect(() => {
    nodesById.current = nodeMap;
  }, [nodeMap]);

  // Choosing a node, link or coverage cell opens the phone sheet, adjusted
  // while rendering so the sheet and the choice appear together.
  const chosen = {
    nodeId: selection?.nodeId ?? null,
    link: chosenLinkKey,
    cell: selectedCell,
  };
  const [shownChoice, setShownChoice] = useState(chosen);
  if (
    chosen.nodeId !== shownChoice.nodeId ||
    chosen.link !== shownChoice.link ||
    chosen.cell !== shownChoice.cell
  ) {
    setShownChoice(chosen);
    const anything =
      chosen.nodeId !== null || chosen.link !== null || chosen.cell !== null;
    if (anything && isNarrowScreen()) setCollapsed(false);
  }

  // Links of the layers switched on, including those without both positions.
  const loadedLinks = useMemo(
    (): SelectedLink[] => [
      ...tracerouteLinks.map((link) => ({
        linkKind: "traceroute" as const,
        link,
      })),
      ...directLinks.map((link) => ({ linkKind: "direct" as const, link })),
      ...neighborLinks.map((link) => ({
        linkKind: "neighbor" as const,
        link,
      })),
    ],
    [tracerouteLinks, directLinks, neighborLinks],
  );
  const loadedByKey = useMemo(
    () =>
      new Map(
        loadedLinks.map((item) => [linkKey(item.linkKind, item.link), item]),
      ),
    [loadedLinks],
  );
  useEffect(() => {
    linksByKey.current = loadedByKey;
  }, [loadedByKey]);
  // A chosen link the filters hide shows as nothing chosen, and comes back
  // when they no longer hide it, as a node does.
  const selectedLink =
    chosenLinkKey === null ? null : (loadedByKey.get(chosenLinkKey) ?? null);
  const nodeTracerouteLinks = useMemo(
    () =>
      selectedNode === null
        ? NO_LINKS
        : tracerouteLinks
            .filter(
              (link) =>
                (link.node_a === selectedNode.node_id ||
                  link.node_b === selectedNode.node_id) &&
                nodeMap.has(link.node_a) &&
                nodeMap.has(link.node_b),
            )
            .sort((a, b) => bothWaysObservations(b) - bothWaysObservations(a)),
    [selectedNode, tracerouteLinks, nodeMap],
  );

  // With a node selected, only nodes within the hop depth over the links
  // switched on stay. Without any link layer there is nothing to walk, so
  // only the node itself stays (and the gateways of its texts, below).
  const anyLinkLayer = Object.values(linkChoice).some(Boolean);
  const focusNodeId = selectedNode?.node_id ?? unplacedNode?.node_id ?? null;
  const hopFocus = useMemo(
    () =>
      focusNodeId === null
        ? null
        : unplacedNode !== null
          ? { nodeIds: [], linkKeys: [] }
          : anyLinkLayer
            ? withinHops(focusNodeId, loadedLinks, hopDepth)
            : { nodeIds: [focusNodeId], linkKeys: [] },
    [focusNodeId, unplacedNode, anyLinkLayer, loadedLinks, hopDepth],
  );

  // The sidebar needs the message count even with Text reach off.
  const reach = useApi<TextReach>(
    selection === null ? null : textReachUrl(selection.nodeId, urls.since),
  );
  const reachShown = selectedNode !== null || unplacedNode !== null;
  const textReach = reachShown && showTextReach ? reach.data : null;
  const textReachError = reachShown && showTextReach ? reach.error : null;
  const messagesSent =
    reach.data === null
      ? reach.error === null
        ? "Loading…"
        : "Could not load"
      : reach.data.messages === 0
        ? "None"
        : reach.data.messages.toLocaleString();
  const textSenders = useApi<TextSenderList>(urls.textSenders, chosenAt);
  // Senders the map draws are listed, and so are senders with no recent
  // position, whose gateways still show; a sender a filter hides is left out.
  const loadedIds = useMemo(
    () => new Set(loadedNodes.map((node) => node.node_id)),
    [loadedNodes],
  );
  const shownTextSenders = useMemo(
    () =>
      textSenders.data === null
        ? null
        : {
            ...textSenders.data,
            items: textSenders.data.items.filter(
              (sender) =>
                nodeMap.has(sender.node_id) || !loadedIds.has(sender.node_id),
            ),
          },
    [textSenders.data, nodeMap, loadedIds],
  );
  const shares = useMemo(
    (): TextShare[] =>
      textReach === null
        ? NO_SHARES
        : textReach.gateways.map((gateway) => ({
            nodeId: gateway.node_id,
            receptions: gateway.receptions,
            messages: textReach.messages,
          })),
    [textReach],
  );
  // A selected link shows only its two nodes, over any node focus; its card
  // replaces the node's, so the badges go too. Otherwise the gateways that
  // reported the selected node's texts stay beside the nodes the hop depth
  // keeps; a sender with no position keeps only its gateways.
  const view = useMemo(
    (): NodeView =>
      selectedLink === null
        ? {
            focus: hopFocus === null ? null : new Set(hopFocus.nodeIds),
            selectedId: focusNodeId,
            shares,
          }
        : {
            focus: new Set([
              selectedLink.link.node_a,
              selectedLink.link.node_b,
            ]),
            selectedId: focusNodeId,
            shares: NO_SHARES,
          },
    [selectedLink, hopFocus, focusNodeId, shares],
  );

  // Coverage cells for the area in view, only where the selected node was
  // heard. The cells shown stay until the next ones arrive. They ignore the
  // time window: surveys are rare drives, kept for months.
  const coverageNode = focusNodeId;
  const coverageUnknown = unknownCoverage && coverageNode === null;
  // Measurements follow their sender's channel; Unknown keeps them all.
  const coverageChannel =
    nodeFilter.channel === ANY || nodeFilter.channel === NOT_REPORTED
      ? null
      : nodeFilter.channel;
  const cellsUrl = (view: CoverageView) =>
    coverageCellsUrl(view, coverageNode, coverageChannel, coverageUnknown);
  const coverage = useApi<CoverageCellList>(
    showCoverage && coverageArea !== null
      ? cellsUrl(coverageArea.fetched)
      : null,
  );
  // When the padded answer is cut short, offscreen cells may have crowded
  // out those in view, so the view alone is asked for and shown instead.
  const coverageInView = useApi<CoverageCellList>(
    coverageArea !== null && coverage.data?.truncated
      ? cellsUrl(coverageArea.view)
      : null,
  );
  const cellDetail = useApi<CoverageCellDetail>(
    showCoverage && selectedCell !== null
      ? coverageCellUrl(
          selectedCell,
          coverageNode,
          coverageChannel,
          coverageUnknown,
        )
      : null,
  );
  // The nodes heard in view, of every node whatever is selected. The last
  // answer stays until the next one arrives.
  const heardAnswer = useApi<CoverageNodeList>(
    (showCoverage || listTab === "heard") && coverageArea !== null
      ? coverageNodesUrl(coverageArea.view, coverageChannel)
      : null,
  );
  const [lastHeard, setLastHeard] = useState<CoverageNodeList | null>(null);
  if (heardAnswer.data !== null && heardAnswer.data !== lastHeard)
    setLastHeard(heardAnswer.data);
  const heardInView = heardAnswer.error === null ? lastHeard : null;
  const coverageCells = coverage.data?.truncated
    ? coverageInView.data
    : coverage.data;
  const coverageError = coverage.error ?? coverageInView.error;
  // Counts are about what is in view, not the padding.
  const coverageView = coverageArea?.view;
  const coverageCellsInView = useMemo(
    () =>
      coverageCells === null || coverageView === undefined
        ? null
        : cellsInView(coverageCells, coverageView),
    [coverageCells, coverageView],
  );
  const coverageSpreadingFactor =
    coverageCellsInView === null
      ? null
      : viewSpreadingFactor(coverageCellsInView.cells);
  // For the cell popup, set up once per style load.
  const coverageSpreadingFactorRef = useRef(coverageSpreadingFactor);
  useEffect(() => {
    coverageSpreadingFactorRef.current = coverageSpreadingFactor;
  }, [coverageSpreadingFactor]);
  const coverageShown = useRef(showCoverage);
  useEffect(() => {
    coverageShown.current = showCoverage;
  }, [showCoverage]);
  const drawnCoverageUnknown = useRef(coverageUnknown);
  useEffect(() => {
    if (
      showCoverage &&
      coverageCells === null &&
      drawnCoverageUnknown.current === coverageUnknown
    )
      return;
    drawnCoverageUnknown.current = coverageUnknown;
    const cells = buildCoverageFeatureCollection(
      showCoverage ? coverageCells : null,
    );
    coverageData.current.cells = cells;
    forgetCellHover.current();
    setSourceData(map.current, COVERAGE_SOURCE, cells);
  }, [showCoverage, coverageCells, coverageUnknown]);
  const shownCell = showCoverage ? cellDetail.data : null;
  useEffect(() => {
    const lines = buildCoverageLines(
      shownCell === null ? null : [shownCell.longitude, shownCell.latitude],
      shownCell?.heard_nodes ?? [],
      nodeMap,
    );
    coverageData.current.lines = lines;
    setSourceData(map.current, COVERAGE_LINES_SOURCE, lines);
  }, [shownCell, nodeMap]);

  useEffect(() => {
    if (page !== null) performance.measure("map: data received");
  }, [page]);

  // Back to the map selects again the node it had selected, where the map
  // already is.
  useEffect(() => {
    writeView(SELECTED_VIEW_KEY, selection?.nodeId ?? null);
  }, [selection]);
  const restoredNodeId = useRef(
    nodeId === null ? readView(SELECTED_VIEW_KEY) : null,
  );
  useEffect(() => {
    const wanted = restoredNodeId.current;
    if (typeof wanted !== "number" || page === null) return;
    restoredNodeId.current = null;
    if (page.items.some((node) => node.node_id === wanted))
      setSelection({ nodeId: wanted, fit: "none" });
  }, [page]);

  // Select the node from #map/<node number> once, when the first nodes arrive;
  // a node the window leaves out is fetched on its own.
  const pendingNodeId = useRef(nodeId);
  useEffect(() => {
    const wanted = pendingNodeId.current;
    if (wanted === null || page === null) return;
    const loaded = page.items.find((node) => node.node_id === wanted);
    if (loaded !== undefined) {
      pendingNodeId.current = null;
      select(loaded);
      return;
    }
    const controller = new AbortController();
    fetchMapNode(wanted, controller.signal)
      .then((linked) => {
        if (controller.signal.aborted) return;
        pendingNodeId.current = null;
        if (linked === null) return;
        setLinkedNode(linked);
        select(linked);
      })
      .catch(() => {
        // The map still works without the linked node.
        if (!controller.signal.aborted) pendingNodeId.current = null;
      });
    return () => controller.abort();
  }, [page, select]);

  // Once another node is chosen or the selection is cleared, the hash no
  // longer names the linked node and the filters apply to it again.
  useEffect(() => {
    if (
      nodeId !== null &&
      pendingNodeId.current === null &&
      selection?.nodeId !== nodeId
    ) {
      setRequestedId(null);
      if (window.location.hash !== "#map")
        window.history.replaceState(window.history.state, "", "#map");
    }
  }, [nodeId, selection]);

  const {
    container: mapContainer,
    map: readyMap,
    failure,
  } = useBasemapMap(
    {
      view: () => savedCamera() ?? { center: [0, 20], zoom: 1 },
      basemap,
      setup: setUpMap,
    },
    [select, clearSelection],
  );

  // The map's handlers, and the node and link layers, which every style load
  // (the first and each basemap switch) adds again.
  function setUpMap(instance: maplibregl.Map) {
    map.current = instance;
    window.sigsleuthMap = instance; // For e2e tests and the devtools console.
    // Bottom right, clear of the filter panel on short screens.
    instance.addControl(new maplibregl.NavigationControl(), "bottom-right");
    let styleLoaded = false;
    // True from the start of a style load, the first or a basemap switch,
    // until it finishes.
    let styleLoading = true;
    let hoveredId: string | number | null = null;
    let hoveredCellId: string | number | null = null;
    const nodeLayers = () => clickableNodeLayers(coverageShown.current);
    const onNodeAt = (point: maplibregl.PointLike) =>
      instance.queryRenderedFeatures(point, {
        layers: [...nodeLayers(), "node-clusters"],
      }).length > 0;
    const cellAt = (point: maplibregl.PointLike) =>
      instance.queryRenderedFeatures(point, {
        layers: [COVERAGE_FILL_LAYER],
      })[0]?.properties;
    const linkNear = (point: maplibregl.Point) =>
      nearestLine(instance, point, LINK_LAYERS);
    let linkHovered = false;
    // Cells are fetched again when the map stops outside the area fetched.
    // A newer request replaces an unfinished one, so no delay is needed.
    const updateCoverageView = () => {
      const bounds = instance.getBounds();
      setCoverageArea((area) =>
        nextCoverageArea(area, {
          west: bounds.getWest(),
          south: bounds.getSouth(),
          east: bounds.getEast(),
          north: bounds.getNorth(),
          zoom: instance.getZoom(),
        }),
      );
    };
    // Where the map is, for Back (this entry) and for the next visit.
    const rememberCamera = () => {
      const { lng, lat } = instance.getCenter();
      const camera: Camera = { center: [lng, lat], zoom: instance.getZoom() };
      writeView(CAMERA_KEY, camera);
      saveSetting(CAMERA_KEY, camera);
    };
    const cellPopup = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      className: "coverage-popup",
      // Clear of the cursor so the cell about to be clicked stays visible.
      offset: 24,
    });
    const highlightCell = (id: string | number | null) => {
      if (id === hoveredCellId) return;
      if (hoveredCellId !== null && instance.getSource(COVERAGE_SOURCE))
        instance.removeFeatureState(
          { source: COVERAGE_SOURCE, id: hoveredCellId },
          "hover",
        );
      if (id !== null)
        instance.setFeatureState(
          { source: COVERAGE_SOURCE, id },
          { hover: true },
        );
      hoveredCellId = id;
    };
    // Shown only where a click would choose the cell.
    const showCellPopup = (event: maplibregl.MapLayerMouseEvent) => {
      const feature = event.features?.[0];
      if (feature === undefined) return;
      if (onNodeAt(event.point)) {
        cellPopup.remove();
        highlightCell(null);
        return;
      }
      instance.getCanvas().style.cursor = "pointer";
      highlightCell(feature.id ?? null);
      cellPopup
        .setLngLat(event.lngLat)
        .setText(
          cellText(feature.properties, coverageSpreadingFactorRef.current),
        )
        .addTo(instance);
    };
    const hideCellPopup = () => {
      cellPopup.remove();
      highlightCell(null);
      if (hoveredId === null && !linkHovered)
        instance.getCanvas().style.cursor = "";
    };
    forgetCellHover.current = () => {
      cellPopup.remove();
      highlightCell(null);
    };
    const highlightNode = (id: string | number | null) => {
      if (instance.getSource("nodes") !== undefined) {
        if (hoveredId !== null)
          instance.removeFeatureState(
            { source: "nodes", id: hoveredId },
            "hover",
          );
        if (id !== null)
          instance.setFeatureState({ source: "nodes", id }, { hover: true });
      }
      hoveredId = id;
    };
    // The ring of overlapping markers; closes on a click elsewhere, Escape,
    // a zoom or a basemap switch.
    const spider = new maplibregl.Marker({
      element: document.createElement("div"),
    });
    let spiderOpen = false;
    const dismissSpider = (event: PointerEvent) => {
      if (
        event.target instanceof Node &&
        !spider.getElement().contains(event.target)
      )
        closeSpider();
    };
    const dismissSpiderOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      closeSpider();
      instance.getCanvas().focus();
    };
    const closeSpider = () => {
      if (!spiderOpen) return;
      spiderOpen = false;
      spider.remove();
      document.removeEventListener("pointerdown", dismissSpider, true);
      document.removeEventListener("keydown", dismissSpiderOnEscape);
    };
    const chooseNode = (node: MapNode) => {
      closeSpider();
      select(node);
      setCollapsed(false);
    };
    const openSpider = (nodes: MapNode[]) => {
      closeSpider();
      hideCellPopup();
      const centre = new maplibregl.LngLat(0, 0);
      for (const node of nodes) {
        centre.lng += node.longitude / nodes.length;
        centre.lat += node.latitude / nodes.length;
      }
      const element = spider.getElement();
      element.replaceChildren(spiderElement(nodes, chooseNode));
      spider.setLngLat(centre).addTo(instance);
      spiderOpen = true;
      document.addEventListener("pointerdown", dismissSpider, true);
      document.addEventListener("keydown", dismissSpiderOnEscape);
    };
    // The nodes whose dots are under a point; a name over a dot does not count.
    const nodesAt = (point: maplibregl.PointLike) => {
      const found = new Map<number, MapNode>();
      for (const feature of instance.queryRenderedFeatures(point, {
        layers: ["node-halos", "nodes"],
      })) {
        const node = nodesById.current.get(feature.properties.nodeId);
        if (node !== undefined) found.set(node.node_id, node);
      }
      return [...found.values()];
    };
    // A click on markers that overlap spreads them into a ring, at any zoom.
    const selectNode = (event: maplibregl.MapMouseEvent) => {
      if (instance.getLayer("nodes") === undefined) return;
      const stacked = nodesAt(event.point);
      if (stacked.length < 2) {
        const top = instance.queryRenderedFeatures(event.point, {
          layers: nodeLayers(),
        })[0];
        const node = nodesById.current.get(top?.properties?.nodeId);
        if (node !== undefined) chooseNode(node);
        return;
      }
      openSpider(stacked);
    };
    // Markers and coverage cells take priority over links.
    const selectLink = (event: maplibregl.MapMouseEvent) => {
      if (onNodeAt(event.point) || cellAt(event.point) !== undefined) return;
      const key = linkNear(event.point)?.properties?.linkKey;
      if (typeof key !== "string") return;
      const selected = linksByKey.current.get(key);
      if (selected === undefined) return;
      setChosenLinkKey(key);
      setSelectedCell(null);
      setCollapsed(false);
      const a = nodesById.current.get(selected.link.node_a);
      const b = nodesById.current.get(selected.link.node_b);
      if (a !== undefined && b !== undefined) focusLink(instance, a, b);
    };
    // The pointer shows wherever a click would take a line.
    const showLinkHover = (event: maplibregl.MapMouseEvent) => {
      const near =
        cellAt(event.point) === undefined &&
        linkNear(event.point) !== undefined;
      if (near === linkHovered) return;
      linkHovered = near;
      if (near) instance.getCanvas().style.cursor = "pointer";
      else if (hoveredId === null) instance.getCanvas().style.cursor = "";
    };
    // MapLibre sends no click after a drag, so only a still click on empty map
    // goes back, one step like the sidebar's back button: from a link or cell
    // to the node selected before, else to nothing selected.
    // A click on a coverage cell without a marker chooses the cell.
    const clearSelectionOnEmptyClick = (event: maplibregl.MapMouseEvent) => {
      if (onNodeAt(event.point)) return;
      const cell = cellAt(event.point);
      if (cell === undefined) {
        if (linkNear(event.point) !== undefined) return;
        if (selectedLinkKey.current !== null) setChosenLinkKey(null);
        else if (cellChosen.current) setSelectedCell(null);
        else clearSelection();
        return;
      }
      setSelectedCell({
        level: Number(cell.level),
        x: Number(cell.x),
        y: Number(cell.y),
      });
      setChosenLinkKey(null);
      setCollapsed(false);
    };
    const expandCluster = (event: maplibregl.MapLayerMouseEvent) => {
      const feature = event.features?.[0];
      const clusterId = feature?.properties?.cluster_id;
      if (typeof clusterId !== "number" || feature?.geometry.type !== "Point")
        return;
      const source = instance.getSource("nodes") as maplibregl.GeoJSONSource;
      // Fit every node in the cluster on screen; a cluster of nodes at one spot
      // stops at zoom 14.
      void source
        .getClusterLeaves(clusterId, Infinity, 0)
        .then((leaves) => {
          if (map.current !== instance) return;
          const bounds = new maplibregl.LngLatBounds();
          for (const leaf of leaves)
            if (leaf.geometry.type === "Point")
              bounds.extend(leaf.geometry.coordinates as [number, number]);
          if (bounds.isEmpty()) return;
          instance.fitBounds(bounds, { padding: 60, maxZoom: 14 });
        })
        .catch(() => {
          if (map.current === instance)
            setMapMessage("The cluster could not expand. Try zooming in.");
        });
    };
    const onHoverEnter = (event: maplibregl.MapLayerMouseEvent) => {
      const feature = event.features?.[0];
      if (feature?.layer.id === "node-labels" && coverageShown.current) return;
      instance.getCanvas().style.cursor = "pointer";
      const id = feature?.id ?? feature?.properties?.cluster_id;
      if (typeof id !== "string" && typeof id !== "number") return;
      highlightNode(id);
    };
    const onHoverLeave = (event: maplibregl.MapLayerMouseEvent) => {
      const cursor = linkHovered ? "pointer" : "";
      if (hoveredId === null) {
        instance.getCanvas().style.cursor = cursor;
        return;
      }
      const stillHovered = instance
        .queryRenderedFeatures(event.point, {
          layers: [...nodeLayers(), "node-clusters"],
        })
        .some((feature) => {
          const id = feature.id ?? feature.properties?.cluster_id;
          return id === hoveredId;
        });
      if (stillHovered) return;
      instance.getCanvas().style.cursor = cursor;
      highlightNode(null);
    };
    // Runs for the first style and again after each basemap switch, which
    // removes every layer; handlers bound to layer ids stay registered.
    const onStyleLoad = () => {
      styleLoaded = true;
      styleLoading = false;
      setMapMessage(null);
      if (instance.getSource("nodes") !== undefined) return;
      hoveredId = null;
      hoveredCellId = null;
      addNodeLayers(
        instance,
        nodeView.current.data,
        findBasemap(shownBasemap.current),
      );
      // Direct links are many, so they go under the neighbour links,
      // which go under the traceroute links.
      addLinkLayers(
        instance,
        "traceroute",
        pendingLinkData.current.traceroute,
        "node-clusters",
      );
      addZeroCostLayer(instance, pendingArrows.current, "node-clusters");
      addLinkLayers(
        instance,
        "neighbor",
        pendingLinkData.current.neighbor,
        linkSourceId("traceroute"),
      );
      addLinkLayers(
        instance,
        "direct",
        pendingLinkData.current.direct,
        linkSourceId("neighbor"),
      );
      // Coverage cells go under every link.
      addCoverageLayers(
        instance,
        coverageData.current.cells,
        coverageData.current.lines,
        linkSourceId("direct"),
      );
      for (const linkKind of LINK_KINDS)
        showLinkFocus(
          instance,
          linkKind,
          linkFocus.current,
          selectedLinkKey.current,
        );
      applyNodeView(
        instance,
        nodeView.current.data,
        nodeView.current.focusActive,
      );
      measureNodeLayersIdle(instance);
    };
    const onLoad = () => {
      for (const layer of NODE_LAYERS) {
        instance.on("mouseenter", layer, onHoverEnter);
        instance.on("mouseleave", layer, onHoverLeave);
      }
      instance.on("click", selectNode);
      instance.on("zoomstart", closeSpider);
      instance.on("click", clearSelectionOnEmptyClick);
      instance.on("click", "node-clusters", expandCluster);
      instance.on("mouseenter", "node-clusters", onHoverEnter);
      instance.on("mouseleave", "node-clusters", onHoverLeave);
      instance.on("click", selectLink);
      instance.on("mousemove", showLinkHover);
      instance.on("mousemove", COVERAGE_FILL_LAYER, showCellPopup);
      instance.on("mouseleave", COVERAGE_FILL_LAYER, hideCellPopup);
      instance.on("moveend", updateCoverageView);
      instance.on("moveend", rememberCamera);
      updateCoverageView();
    };
    const onStyleLoading = () => {
      closeSpider();
      styleLoading = true;
    };
    // Only the style or the basemap's own sources failing is the basemap's
    // fault; a mistake in our layers is logged for developers instead. Before
    // the first style loads, useBasemapMap reports the failure.
    const showBasemapError = (
      event: maplibregl.ErrorEvent & { sourceId?: string },
    ) => {
      const ourSource =
        event.sourceId === "nodes" ||
        COVERAGE_SOURCES.includes(event.sourceId ?? "") ||
        event.sourceId === ZERO_COST_LAYER ||
        LINK_KINDS.some(
          (linkKind) => event.sourceId === linkSourceId(linkKind),
        );
      if (!styleLoading && (event.sourceId === undefined || ourSource)) {
        console.error(event.error);
        return;
      }
      if (styleLoaded) {
        setMapMessage(
          "The basemap could not fully load. Available node positions are still shown.",
        );
      }
    };
    instance.on("style.load", onStyleLoad);
    instance.once("load", onLoad);
    instance.on("styledataloading", onStyleLoading);
    instance.on("error", showBasemapError);
    const resizeObserver = new ResizeObserver(() => instance.resize());
    resizeObserver.observe(instance.getContainer());
    return () => {
      resizeObserver.disconnect();
      instance.off("style.load", onStyleLoad);
      instance.off("load", onLoad);
      for (const layer of NODE_LAYERS) {
        instance.off("mouseenter", layer, onHoverEnter);
        instance.off("mouseleave", layer, onHoverLeave);
      }
      instance.off("click", selectNode);
      instance.off("zoomstart", closeSpider);
      instance.off("click", clearSelectionOnEmptyClick);
      instance.off("click", "node-clusters", expandCluster);
      instance.off("mouseenter", "node-clusters", onHoverEnter);
      instance.off("mouseleave", "node-clusters", onHoverLeave);
      instance.off("click", selectLink);
      instance.off("mousemove", showLinkHover);
      instance.off("mousemove", COVERAGE_FILL_LAYER, showCellPopup);
      instance.off("mouseleave", COVERAGE_FILL_LAYER, hideCellPopup);
      instance.off("moveend", updateCoverageView);
      instance.off("moveend", rememberCamera);
      cellPopup.remove();
      forgetCellHover.current = () => {};
      closeSpider();
      instance.off("styledataloading", onStyleLoading);
      instance.off("error", showBasemapError);
      map.current = null;
      if (window.sigsleuthMap === instance) delete window.sigsleuthMap;
    };
  }

  useEffect(() => {
    const instance = map.current;
    if (instance === null || shownBasemap.current === basemap) return;
    shownBasemap.current = basemap;
    // Without diff the old style is replaced whole, so "style.load" re-adds our layers.
    instance.setStyle(findBasemap(basemap).style, { diff: false });
  }, [basemap]);

  // Every change to the nodes or how they show rebuilds their data, so the
  // layers need no other updates.
  useEffect(() => {
    const data = buildNodeFeatureCollection(items, view);
    const focusActive = view.focus !== null;
    nodeView.current = { data, focusActive };
    const instance = map.current;
    if (instance?.getSource("nodes") === undefined) return;
    applyNodeView(instance, data, focusActive);
    measureNodeLayersIdle(instance);
  }, [items, view]);

  const tracerouteData = linkLayers.traceroute.data;
  const directData = linkLayers.direct.data;
  const neighborData = linkLayers.neighbor.data;
  useEffect(() => {
    showLinkData(
      map.current,
      pendingLinkData.current,
      "traceroute",
      tracerouteData,
    );
  }, [tracerouteData]);
  const zeroCostData = linkLayers.traceroute.arrows;
  useEffect(() => {
    pendingArrows.current = zeroCostData;
    setSourceData(map.current, ZERO_COST_LAYER, zeroCostData);
  }, [zeroCostData]);
  useEffect(() => {
    showLinkData(map.current, pendingLinkData.current, "direct", directData);
  }, [directData]);
  useEffect(() => {
    showLinkData(
      map.current,
      pendingLinkData.current,
      "neighbor",
      neighborData,
    );
  }, [neighborData]);

  useEffect(() => {
    cellChosen.current = selectedCell !== null;
  }, [selectedCell]);
  // A selected link shows only itself, over any node focus.
  const hopLinkKeys = hopFocus?.linkKeys ?? null;
  useEffect(() => {
    if (selectedLink === null) {
      selectedLinkKey.current = null;
      linkFocus.current = hopLinkKeys;
    } else {
      const key = linkKey(selectedLink.linkKind, selectedLink.link);
      selectedLinkKey.current = key;
      linkFocus.current = [key];
    }
    const instance = map.current;
    if (instance?.getSource(linkSourceId("traceroute")) === undefined) return;
    for (const linkKind of LINK_KINDS)
      showLinkFocus(
        instance,
        linkKind,
        linkFocus.current,
        selectedLinkKey.current,
      );
  }, [selectedLink, hopLinkKeys]);

  // Wait for enabled layers, then fit once per choice; filters and Back keep
  // the camera where the user left it.
  const linksReady = !(
    tracerouteLayer.loading ||
    directLayer.loading ||
    neighborLayer.loading
  );
  function nodesOnFocusedLinks(): MapNode[] {
    const keys = new Set(hopFocus?.linkKeys);
    const ends = new Map<number, MapNode>();
    for (const { linkKind, link } of loadedLinks) {
      if (!keys.has(linkKey(linkKind, link))) continue;
      const a = nodeMap.get(link.node_a);
      const b = nodeMap.get(link.node_b);
      if (a !== undefined && b !== undefined) {
        ends.set(a.node_id, a);
        ends.set(b.node_id, b);
      }
    }
    return [...ends.values()];
  }
  const extentUrl =
    (selection?.fit === "coverage" || selection?.fit === "view") && showCoverage
      ? coverageExtentUrl(selection.nodeId, coverageChannel)
      : null;
  const extent = useApi<CoverageExtent | null>(extentUrl);
  const fitReady =
    !extent.loading &&
    (selection?.fit === "view"
      ? !reach.loading && (unplacedNode !== null || linksReady)
      : selection?.fit === "coverage" && showCoverage);
  const fittedSelection = useRef<Selection | null>(null);
  const fitSelection = useEffectEvent(() => {
    const instance = readyMap;
    if (
      instance === null ||
      (selectedNode === null && unplacedNode === null) ||
      selectedLink !== null
    )
      return;
    const otherNodes =
      selection?.fit === "view"
        ? [
            ...nodesOnFocusedLinks(),
            ...(textReach?.gateways ?? []).flatMap((gateway) => {
              const node = nodeMap.get(gateway.node_id);
              return node === undefined ? [] : [node];
            }),
          ]
        : NO_NODES;
    fittedSelection.current = selection;
    focusCoverage(
      instance,
      selectedNode,
      extent.data,
      selection?.zoomIn ?? false,
      otherNodes,
      selection?.coverageCell,
    );
  });
  useEffect(() => {
    if (!fitReady || fittedSelection.current === selection) return;
    fitSelection();
  }, [selection, fitReady, readyMap, selectedNode, selectedLink, unplacedNode]);

  // Choosing a chatty node is asking for its text reach.
  function chooseTextSender(sender: TextSender) {
    setUnknownCoverage(false);
    setShowTextReach(true);
    const node = nodeMap.get(sender.node_id);
    if (node !== undefined) {
      select(node);
      return;
    }
    setSelection({ nodeId: sender.node_id, fit: "view", unplaced: sender });
    setChosenLinkKey(null);
    setSelectedCell(null);
  }

  function chooseCoverageNode(heard: CoverageViewNode) {
    setUnknownCoverage(false);
    setShowCoverage(true);
    const node = nodeMap.get(heard.node_id);
    if (node !== undefined) {
      select(node);
      return;
    }
    setSelection({
      nodeId: heard.node_id,
      fit: "coverage",
      zoomIn: true,
      unplaced: heard,
    });
    setChosenLinkKey(null);
    setSelectedCell(null);
  }

  function chooseUnknownCoverage(extent: CoverageExtent | null) {
    if (unknownCoverage) {
      setUnknownCoverage(false);
      setSelectedCell(null);
      return;
    }
    if (extent === null) return;
    clearSelection();
    setSelectedCell(null);
    setShowCoverage(true);
    setUnknownCoverage(true);
    if (readyMap !== null) focusCoverage(readyMap, null, extent, true);
  }

  function chooseSeenWindow(window: SeenWindow) {
    setSelectedCell(null);
    setLinkedNode(null);
    setRequestedId(null);
    setSeenWindow(window);
    setShownWindow(mapWindowStart(window));
  }

  function toggleLinkLayer(linkKind: LinkKind, shown: boolean) {
    setLinkChoice({ ...linkChoice, [linkKind]: shown });
  }

  // Switching coverage on shows its list of heard nodes, unless a choice is
  // shown: its back button and ‹ › keep to the list it came from.
  function toggleCoverage(on: boolean) {
    setShowCoverage(on);
    if (!on) setSelectedCell(null);
    else if (shown === null) setListTab("heard");
  }

  function openFilters(open: boolean) {
    setFiltersOpen(open);
    if (open && isNarrowScreen()) setCollapsed(true);
  }

  function chooseHeardNode(nodeId: number) {
    setUnknownCoverage(false);
    setSelection({
      nodeId,
      fit: "coverage",
      zoomIn: true,
      coverageCell: selectedCell ?? undefined,
    });
    setChosenLinkKey(null);
    setSelectedCell(null);
  }

  // Nodes kept by the hop depth that are drawn, besides the selected one.
  const nearbyShown =
    hopFocus === null
      ? 0
      : hopFocus.nodeIds.filter((id) => id !== focusNodeId && nodeMap.has(id))
          .length;

  // Only drawn links can be selected, so both nodes are normally on the map.
  const selectedLinkA =
    selectedLink === null ? undefined : nodeMap.get(selectedLink.link.node_a);
  const selectedLinkB =
    selectedLink === null ? undefined : nodeMap.get(selectedLink.link.node_b);
  const selectedLinkNodes =
    selectedLink !== null &&
    selectedLinkA !== undefined &&
    selectedLinkB !== undefined
      ? { ...selectedLink, a: selectedLinkA, b: selectedLinkB }
      : null;
  const shownPosition =
    detailNode === null ||
    (detailNode.position === null && detailNode.estimate === null)
      ? null
      : positionSource(detailNode);
  const positionAge =
    detailNode?.position == null || detailNode.estimate !== null ? null : (
      <time
        dateTime={detailNode.position.observed_at}
        title={new Date(detailNode.position.observed_at).toLocaleString()}
      >
        {formatHours(
          (chosenAt - Date.parse(detailNode.position.observed_at)) / 3_600_000,
        )}{" "}
        ago
      </time>
    );

  // The sidebar shows the node lists, or else the latest choice in their
  // place: a link or coverage cell over the selected node, or the node.
  const shown: "link" | "cell" | "node" | null =
    selectedLinkNodes !== null
      ? "link"
      : showCoverage && selectedCell !== null
        ? "cell"
        : selectedNode !== null || unplacedNode !== null
          ? "node"
          : null;
  const activeTab = listTab;
  const listName = activeTab === "heard" ? "Coverage" : "Chatty nodes";
  const viewedId = focusNodeId;
  const viewedName =
    selectedNode !== null
      ? nodeLabel(selectedNode)
      : unplacedNode !== null
        ? (unplacedNode.long_name ?? formatNodeId(unplacedNode.node_id))
        : null;
  // Back goes one step: from a link or cell to the node under it, if any.
  const backTo =
    shown === "node" || viewedName === null ? listName : viewedName;
  function goBack() {
    if (shown === "link") setChosenLinkKey(null);
    else if (shown === "cell") setSelectedCell(null);
    else clearSelection();
  }

  // Coverage keeps the list it had while a choice shows, as fitting
  // each node moves the view, so ‹ › walk a steady list.
  const [heardShown, setHeardShown] = useState(heardInView);
  if (shown === null && heardShown !== heardInView) setHeardShown(heardInView);
  // ‹ › step through the open list's nodes that can be chosen.
  const stepIds =
    activeTab === "heard"
      ? (heardShown?.nodes ?? [])
          .filter(
            (node) => nodeMap.has(node.node_id) || !loadedIds.has(node.node_id),
          )
          .map((node) => node.node_id)
      : (shownTextSenders?.items ?? []).map((sender) => sender.node_id);
  const stepIndex =
    shown === "link"
      ? nodeTracerouteLinks.findIndex(
          (link) => linkKey("traceroute", link) === chosenLinkKey,
        )
      : shown === "node" && viewedId !== null
        ? stepIds.indexOf(viewedId)
        : -1;
  const details = useRef<HTMLDivElement>(null);
  const nodeLinkScroll = useRef<{ nodeId: number; top: number } | null>(null);
  useEffect(() => {
    if (shown !== "node" || nodeLinkScroll.current === null) return;
    if (details.current !== null && nodeLinkScroll.current.nodeId === viewedId)
      details.current.scrollTop = nodeLinkScroll.current.top;
    nodeLinkScroll.current = null;
  }, [shown, viewedId]);
  function openNodeLink(key: string) {
    if (shown === "node" && viewedId !== null)
      nodeLinkScroll.current = {
        nodeId: viewedId,
        top: details.current?.scrollTop ?? 0,
      };
    if (details.current !== null) details.current.scrollTop = 0;
    setChosenLinkKey(key);
    setSelectedCell(null);
    const chosen = loadedByKey.get(key);
    if (readyMap === null || chosen === undefined) return;
    const a = nodeMap.get(chosen.link.node_a);
    const b = nodeMap.get(chosen.link.node_b);
    if (a !== undefined && b !== undefined) focusLink(readyMap, a, b);
  }
  function stepTo(index: number) {
    if (shown === "link") {
      openNodeLink(linkKey("traceroute", nodeTracerouteLinks[index]));
      return;
    }
    const nodeId = stepIds[index];
    if (activeTab === "heard") {
      const node = heardShown?.nodes.find((node) => node.node_id === nodeId);
      if (node !== undefined) chooseCoverageNode(node);
      return;
    }
    const sender = shownTextSenders?.items.find(
      (item) => item.node_id === nodeId,
    );
    if (sender !== undefined) chooseTextSender(sender);
  }

  // Back in the lists, the row of the node shown last is scrolled into view.
  const [lastViewedId, setLastViewedId] = useState<number | null>(null);
  if (viewedId !== null && viewedId !== lastViewedId) setLastViewedId(viewedId);
  const lists = useRef<HTMLDivElement | null>(null);
  const listsShown = shown === null && !collapsed;
  useEffect(() => {
    if (!listsShown || lastViewedId === null) return;
    lists.current
      ?.querySelector(`[data-node-id="${lastViewedId}"]`)
      ?.scrollIntoView?.({ block: "nearest" });
  }, [listsShown, lastViewedId, activeTab]);

  const selectedCoverage =
    showCoverage && selection !== null ? (
      <SelectedCoverage
        cells={coverageCellsInView}
        onFit={() =>
          setSelection({ ...selection, fit: "coverage", zoomIn: true })
        }
      />
    ) : null;

  return (
    <section
      className={`map-workspace ${collapsed ? "sidebar-collapsed" : ""}`}
      aria-label="Node map"
    >
      <div className="map-surface">
        <div
          ref={mapContainer}
          className="map-canvas"
          style={{ visibility: failure !== null ? "hidden" : undefined }}
          aria-label="Interactive map of latest valid node positions"
        />
        <div className="map-overlays">
          {loading ? <StatusPanel>Loading positions…</StatusPanel> : null}
          {error !== null ? (
            <StatusPanel tone="error">
              {error} Reload the page to try again.
            </StatusPanel>
          ) : null}
          {page?.truncated ? (
            <StatusPanel>
              Only the {MAP_NODE_LIMIT.toLocaleString()} most recently seen
              nodes are shown.
            </StatusPanel>
          ) : null}
          {page !== null && loadedNodes.length === 0 ? (
            <StatusPanel>No recent nodes with valid positions.</StatusPanel>
          ) : null}
          {loadedNodes.length > 0 && items.length === 0 ? (
            <StatusPanel>No nodes match the node filters.</StatusPanel>
          ) : null}
          {LINK_KINDS.map((linkKind) => {
            const { error, page } = linkLayers[linkKind];
            const name = LINK_NAMES[linkKind];
            return (
              <Fragment key={linkKind}>
                {error !== null ? (
                  <StatusPanel tone="error">
                    {capitalize(name)} could not load. {error}
                  </StatusPanel>
                ) : null}
                {page?.truncated ? (
                  <StatusPanel>
                    Only the {MAP_LINK_LIMIT.toLocaleString()} most recently
                    seen {name} are shown.
                  </StatusPanel>
                ) : null}
              </Fragment>
            );
          })}
          {showCoverage && coverageError !== null ? (
            <StatusPanel tone="error">
              Coverage could not load. {coverageError}
            </StatusPanel>
          ) : null}
          {showCoverage && coverageCells?.truncated ? (
            <StatusPanel>
              Only the busiest coverage cells are shown. Zoom in to see all.
            </StatusPanel>
          ) : null}
          {textReachError !== null ? (
            <StatusPanel tone="error">
              Text reach could not load. {textReachError}
            </StatusPanel>
          ) : null}
          {failure !== null ? (
            <StatusPanel tone="error">
              {failure === "basemap"
                ? "The map could not load. Node positions cannot be shown yet. Refresh the page to try again."
                : MAP_FAILURE_MESSAGES[failure]}
            </StatusPanel>
          ) : mapMessage !== null ? (
            <StatusPanel tone="error">{mapMessage}</StatusPanel>
          ) : null}
        </div>
        <MapFilterPanel
          open={filtersOpen}
          onOpen={openFilters}
          seenWindow={shownWindow.window}
          onSeenWindow={chooseSeenWindow}
          nodeFilter={nodeFilter}
          onNodeFilter={setNodeFilter}
          categoryCounts={counts.categories}
          roles={counts.roles}
          channels={counts.channels}
          estimatedCount={counts.estimated}
          linkChoice={linkChoice}
          onLinkLayer={toggleLinkLayer}
          linkTiers={linkTiers}
          onLinkTiers={setLinkTiers}
          showTextReach={showTextReach}
          onShowTextReach={setShowTextReach}
          showCoverage={showCoverage}
          onShowCoverage={toggleCoverage}
          coverageSpreadingFactor={coverageSpreadingFactor}
          minObservations={minObservationsText}
          onMinObservations={setMinObservationsText}
          minKm={minKm}
          onMinKm={setMinKm}
          hopDepth={hopDepth}
          onHopDepth={setHopDepth}
          nearbyShown={focusNodeId === null ? null : nearbyShown}
        />
        <BasemapSwitcher value={basemap} onChange={setBasemap} />
        {selectedNode !== null && selectedLinkNodes === null ? (
          <div className="map-share">
            <ShareButton
              hash={mapViewHref(selectedNode.node_id, {
                show: [
                  ...LINK_KINDS.filter((kind) => linkChoice[kind]),
                  ...(showTextReach ? (["reach"] as const) : []),
                  ...(showCoverage ? (["coverage"] as const) : []),
                ],
                window: shownWindow.window,
                channel: nodeFilter.channel === ANY ? null : nodeFilter.channel,
              })}
              title={nodeLabel(selectedNode)}
            />
          </div>
        ) : null}
      </div>
      <aside className="map-sidebar" aria-label="Map sidebar">
        <div className="sidebar-handle">
          <IconButton
            icon={collapsed ? "expand" : "collapse"}
            label={collapsed ? "Expand map sidebar" : "Collapse map sidebar"}
            aria-expanded={!collapsed}
            aria-controls="map-sidebar-content"
            onClick={() => setCollapsed((value) => !value)}
          />
        </div>
        <div
          id="map-sidebar-content"
          className="sidebar-content"
          hidden={collapsed}
        >
          <NodeSearch nodes={items} onSelect={select} keepResults />
          <div ref={lists} className="sidebar-lists" hidden={shown !== null}>
            <SidebarTabs
              tabs={[
                {
                  tab: "chatty",
                  label: "Chatty nodes",
                  count:
                    shownTextSenders?.items.length.toLocaleString() ?? null,
                },
                {
                  tab: "heard",
                  label: "Coverage",
                  count:
                    heardShown === null
                      ? null
                      : `${heardShown.nodes.length.toLocaleString()}${heardShown.truncated ? "+" : ""}`,
                },
              ]}
              active={activeTab}
              onChange={setListTab}
              idPrefix="map-list"
            />
            {activeTab === "heard" ? (
              <HeardInView
                id="map-list-heard"
                page={heardShown}
                error={heardAnswer.error}
                selectedNodeId={viewedId}
                drawn={nodeMap}
                loadedIds={loadedIds}
                onChoose={chooseCoverageNode}
                onChooseUnknown={chooseUnknownCoverage}
                unknownSelected={coverageUnknown}
              />
            ) : (
              <ChattyNodes
                id="map-list-chatty"
                page={shownTextSenders}
                error={textSenders.error}
                selectedNodeId={viewedId}
                placedIds={loadedIds}
                onChoose={chooseTextSender}
              />
            )}
          </div>
          {shown === null ? null : (
            <div ref={details} className="sidebar-details">
              <SidebarNav
                backTo={backTo}
                onBack={goBack}
                step={
                  stepIndex < 0
                    ? undefined
                    : {
                        index: stepIndex,
                        total:
                          shown === "link"
                            ? nodeTracerouteLinks.length
                            : stepIds.length,
                        noun: shown === "link" ? "link" : "node",
                        onStep: stepTo,
                      }
                }
              />
              {shown === "link" && selectedLinkNodes !== null ? (
                <section className="selected-node" aria-live="polite">
                  <MapLinkDetails
                    linkKind={selectedLinkNodes.linkKind}
                    link={selectedLinkNodes.link}
                    nodeA={selectedLinkNodes.a}
                    nodeB={selectedLinkNodes.b}
                    selectedNodeId={focusNodeId}
                    km={
                      // Shown between reported positions only: a link to an
                      // estimate mostly measures the estimate.
                      selectedLinkNodes.a.estimate === null &&
                      selectedLinkNodes.b.estimate === null
                        ? drawnKm(selectedLinkNodes.a, selectedLinkNodes.b)
                        : null
                    }
                    since={urls.since}
                  />
                </section>
              ) : shown === "cell" ? (
                <CoverageCellDetails
                  detail={cellDetail}
                  isDrawn={(nodeId) => nodeMap.has(nodeId)}
                  onChooseNode={chooseHeardNode}
                />
              ) : detailNode === null && unplacedNode !== null ? (
                <>
                  <StatusPanel
                    tone={nodeDetails.error === null ? undefined : "error"}
                  >
                    {nodeDetails.error ??
                      (nodeDetails.loading
                        ? "Loading node…"
                        : "No current node details.")}
                  </StatusPanel>
                  {selectedCoverage}
                </>
              ) : detailNode !== null ? (
                <section className="selected-node" aria-live="polite">
                  <div className="selected-node-heading">
                    <span
                      className="node-avatar avatar-green"
                      title="Short name"
                    >
                      {nodeMarkerLabel(detailNode)}
                    </span>
                    <div>
                      <h3>{nodeLabel(detailNode)}</h3>
                      <span className="muted">
                        <CopyValue
                          value={formatNodeId(detailNode.node_id)}
                          label="node ID"
                        />
                      </span>
                    </div>
                    <a
                      className="icon-button"
                      href={nodeHref(detailNode.node_id)}
                      aria-label="View node details"
                      title="View node details"
                    >
                      <Icon name="open" />
                    </a>
                  </div>
                  <dl className="node-summary node-card-facts">
                    <div>
                      <dt>Role</dt>
                      <dd className={detailNode.role === null ? "muted" : ""}>
                        {formatRole(detailNode.role)}
                        {detailNode.last_gateway_at == null ? null : (
                          <span
                            className="gateway-note"
                            title={`Last reported a packet ${new Date(detailNode.last_gateway_at).toLocaleString()}`}
                          >
                            (Gateway)
                          </span>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Channel</dt>
                      <dd
                        className={detailNode.channel === null ? "muted" : ""}
                      >
                        {detailNode.channel ?? "Unknown"}
                      </dd>
                    </div>
                    <div>
                      <dt>Hardware model</dt>
                      <dd
                        className={
                          detailNode.hardware_model === null ? "muted" : ""
                        }
                      >
                        {formatHardware(detailNode.hardware_model)}
                      </dd>
                    </div>
                    <div>
                      <dt>Position</dt>
                      <dd
                        className={shownPosition === null ? "muted" : ""}
                        title={shownPosition?.hover ?? undefined}
                      >
                        {shownPosition?.text ?? "Unknown"}
                        {positionAge === null ? null : (
                          <>
                            ,{" "}
                            {detailNode.position?.packet_row_id == null ? (
                              positionAge
                            ) : (
                              <a
                                href={`#packets/${detailNode.position.packet_row_id}`}
                              >
                                {positionAge}
                              </a>
                            )}
                          </>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Altitude</dt>
                      {detailNode.position?.altitude_meters == null ? (
                        <dd className="muted">Unknown</dd>
                      ) : (
                        <dd>{detailNode.position.altitude_meters} m</dd>
                      )}
                    </div>
                    <div>
                      <dt>Last seen</dt>
                      <dd>
                        <ShortTime value={detailNode.last_seen_at} />
                      </dd>
                    </div>
                    <div>
                      <dt>Last sent</dt>
                      {detailNode.last_sent_at === null ? (
                        <dd className="muted">No own packet observed</dd>
                      ) : (
                        <dd>
                          {/* The time opens the packet, so the row stays one line. */}
                          {detailNode.last_sent_packet_row_id === null ? (
                            <ShortTime value={detailNode.last_sent_at} />
                          ) : (
                            <a
                              href={`#packets/${detailNode.last_sent_packet_row_id}`}
                            >
                              <ShortTime value={detailNode.last_sent_at} />
                            </a>
                          )}
                        </dd>
                      )}
                    </div>
                    <div>
                      <dt title="Broadcast text messages">Messages sent</dt>
                      <dd>{messagesSent}</dd>
                    </div>
                  </dl>
                  {selectedCoverage}
                  {selectedNode === null ? null : (
                    <NodeTracerouteLinks
                      nodeId={selectedNode.node_id}
                      links={nodeTracerouteLinks}
                      nodes={nodeMap}
                      onOpen={openNodeLink}
                    />
                  )}
                </section>
              ) : null}
            </div>
          )}
        </div>
      </aside>
    </section>
  );
}

type ListTab = "chatty" | "heard";

function NodeTracerouteLinks({
  nodeId,
  links,
  nodes,
  onOpen,
}: {
  nodeId: number;
  links: MapLink[];
  nodes: ReadonlyMap<number, MapNode>;
  onOpen: (key: string) => void;
}) {
  if (links.length === 0) return null;
  return (
    <div className="node-traceroute-links">
      <h4 className="section-label">Traceroute hops</h4>
      <table>
        <thead>
          <tr>
            <th scope="col">Node</th>
            <th scope="col" title="Times heard out and in">
              Seen
            </th>
            <th scope="col" title="Weaker direction's signal margin">
              Margin
            </th>
            <th scope="col">km</th>
          </tr>
        </thead>
        <tbody>
          {links.map((raw) => {
            const key = linkKey("traceroute", raw);
            const link = fromNode(raw, nodeId);
            const other = nodes.get(link.node_b)!;
            const origin = nodes.get(nodeId)!;
            const km =
              origin.estimate === null && other.estimate === null
                ? drawnKm(origin, other)
                : null;
            const margin = weakerMargin(link);
            return (
              <tr
                key={key}
                onClick={(event) => {
                  if ((event.target as Element).closest("button")) return;
                  onOpen(key);
                }}
              >
                <td>
                  <button
                    type="button"
                    className="text-button"
                    title={nodeLabel(other)}
                    aria-label={`Show link to ${nodeLabel(other)}`}
                    onClick={() => onOpen(key)}
                  >
                    {shortLabel(other.node_id, other.short_name)}
                  </button>
                </td>
                <td>
                  <BothWays
                    left={link.a_to_b.observations.toLocaleString()}
                    right={link.b_to_a.observations.toLocaleString()}
                  />
                </td>
                <td>
                  {margin === null ? "—" : <MarginValue value={margin} />}
                </td>
                <td>{formatKm(km)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SelectedCoverage({
  cells,
  onFit,
}: {
  cells: CoverageCellList | null;
  onFit: () => void;
}) {
  let measurements = 0;
  let guessed = 0;
  for (const cell of cells?.cells ?? []) {
    measurements += cell.measurements;
    guessed += cell.guessed;
  }
  return (
    <div className="selected-coverage">
      {cells === null ? null : (
        <p className="sidebar-note">
          {measurements === 0
            ? "No measurements in view."
            : `${guessed.toLocaleString()} of ${measurements.toLocaleString()} measurements in view identified using MQTT evidence.`}
        </p>
      )}
      <button type="button" className="text-button" onClick={onFit}>
        <Icon name="locate" /> Fit map to coverage
      </button>
    </div>
  );
}

// The background map menu, opened from a layers button on the map.
function BasemapSwitcher({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="basemap-switcher">
      <IconButton
        icon="layers"
        label="Background map"
        aria-expanded={open}
        aria-controls="map-basemap-menu"
        onClick={() => setOpen((value) => !value)}
      />
      {open ? (
        <div
          id="map-basemap-menu"
          className="basemap-menu"
          role="radiogroup"
          aria-labelledby="map-basemap-heading"
        >
          <p id="map-basemap-heading" className="section-label">
            Background map
          </p>
          {BASEMAPS.map((option) => (
            <label key={option.id}>
              <input
                type="radio"
                name="map-basemap"
                value={option.id}
                checked={option.id === value}
                onChange={() => {
                  onChange(option.id);
                  setOpen(false);
                }}
              />
              {option.label}
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// Keeps the latest data for the next style load and shows it now if the map is ready.
function showLinkData(
  instance: maplibregl.Map | null,
  pending: Record<LinkKind, LinkFeatureCollection>,
  linkKind: LinkKind,
  data: LinkFeatureCollection,
) {
  pending[linkKind] = data;
  const source = instance?.getSource(linkSourceId(linkKind)) as
    maplibregl.GeoJSONSource | undefined;
  source?.setData(data);
}

function setSourceData(
  instance: maplibregl.Map | null,
  sourceId: string,
  data: GeoJSON.GeoJSON,
) {
  const source = instance?.getSource(sourceId) as
    maplibregl.GeoJSONSource | undefined;
  source?.setData(data);
}

function capitalize(text: string) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
