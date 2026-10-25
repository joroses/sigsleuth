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
  type LinkKind,
  MAP_LINK_LIMIT,
  MAP_NODE_LIMIT,
  MapLink,
  MapLinkList,
  MapNode,
  MapNodeList,
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
  type CoverageNodeMode,
  type CoverageArea,
  type CoverageView,
} from "./coverageLayer";
import { HeardInView } from "./HeardInView";
import { MapFilterPanel } from "./MapFilterPanel";
import { MapLinkDetails } from "./MapLinkDetails";
import {
  LINK_KINDS,
  LINK_LAYERS,
  LINK_MIN_ZOOM,
  MAX_HOP_DEPTH,
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
  showNodeNames,
  buildNodeFeatureCollection,
  type NodeFeatureCollection,
  type NodeView,
  type TextShare,
} from "./mapNodeLayers";
import {
  SPIDER_MIN_ZOOM,
  SPIDER_OVERLAP_PX,
  spiderElement,
} from "./mapNodeSpider";
import { ChattyNodes } from "./ChattyNodes";
import { nearestLine } from "./lineHit";
import { isNarrowScreen } from "./narrowScreen";
import {
  estimateText,
  mapReportPositionText,
  formatNodeId,
  formatRole,
  nodeHref,
  nodeLabel,
  nodeMarkerLabel,
  shortLabel,
} from "./nodeDisplay";
import {
  ANY,
  busiestChannel,
  DEFAULT_FILTER,
  filterCounts,
  linkableNodes,
  NOT_REPORTED,
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
  useSetting,
  useViewState,
  writeView,
} from "./remember";
import { useApi } from "./useApi";
import { MAP_FAILURE_MESSAGES, useBasemapMap } from "./useBasemapMap";
import { CopyValue, Icon, IconButton, StatusPanel, ShortTime } from "./ui";

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

// Center the node and zoom in far enough to show about 300 km around it, keeping a
// closer zoom if the map already has one.
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
  instance.flyTo({
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
    { padding: 60 },
  );
  if (camera === undefined) return;
  instance.flyTo({
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
  const camera = instance.cameraForBounds(bounds, { padding: 60 });
  if (camera === undefined) return;
  instance.flyTo({
    center: camera.center,
    zoom: Math.max(camera.zoom ?? 0, TEXT_REACH_MIN_ZOOM),
    offset: sheetOffset(instance),
  });
}

// Show a node and its coverage cells: only when some are out of view, and
// without zooming in, unless zoomIn fits them whatever the view.
function focusCoverage(
  instance: maplibregl.Map,
  node: MapNode,
  extent: CoverageExtent,
  zoomIn: boolean,
) {
  const bounds = new maplibregl.LngLatBounds(
    [extent.west, extent.south],
    [extent.east, extent.north],
  ).extend([node.longitude, node.latitude]);
  const view = instance.getBounds();
  if (
    !zoomIn &&
    view.contains(bounds.getSouthWest()) &&
    view.contains(bounds.getNorthEast())
  )
    return;
  const camera = instance.cameraForBounds(bounds, { padding: 60 });
  if (camera === undefined) return;
  instance.flyTo({
    center: camera.center,
    zoom: Math.min(camera.zoom ?? 0, zoomIn ? 14 : instance.getZoom()),
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

/**
 * The chosen node and how the camera follows the choice: "node" centres it at
 * once, "reach" fits it and its gateways once its text reach loads,
 * "coverage" fits its coverage cells once their extent loads. Every
 * choice is a new object, so choosing the same node again fits again, and any
 * other choice drops a fit still waiting.
 */
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

interface Selection {
  nodeId: number;
  fit: "node" | "reach" | "coverage";
  /** Set for a chatty node with no recent position, which is not drawn. */
  unplaced?: TextSender;
  /** Fit "coverage" zooms in too: the Fit map to coverage button. */
  zoomIn?: boolean;
}

/**
 * Whether a link is on the channel chosen in the map filters. Every link has
 * a channel, so "not reported" keeps them all.
 */
function onChosenChannel(link: MapLink, channel: string): boolean {
  return (
    channel === ANY || channel === NOT_REPORTED || link.channel === channel
  );
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
  const { data: page, error } = useApi<MapLinkList>(url, chosenAt);
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
  return { page, error, links, data, arrows };
}

const NO_ZERO_COST: ZeroCostHop[] = [];

// "idle" follows the source update and rendered layers; scripts/perf-map.sh reads it.
function measureNodeLayersIdle(instance: maplibregl.Map) {
  instance.once("idle", () => performance.measure("map: node layers idle"));
}

/** nodeId, from #map/<node number>, is selected once nodes load. */
export function MapView({ nodeId = null }: { nodeId?: number | null }) {
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
  // The only nodes named, for the next style load; null names them all.
  const namedNodes = useRef<readonly number[] | null>(null);
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
    () => mapWindowStart(seenWindow),
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
  const [nodeFilter, setNodeFilter] = useSetting("nodeFilter", DEFAULT_FILTER);
  // A first visit starts on the busiest channel once the nodes load.
  const [channelPending, setChannelPending] = useState(firstVisit);
  if (channelPending && page !== null) {
    setChannelPending(false);
    const channel = busiestChannel(page.items);
    if (channel !== null) setNodeFilter((filter) => ({ ...filter, channel }));
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
  const [linkChoice, setLinkChoice] = useSetting(
    "linkLayers",
    DEFAULT_LAYER_CHOICE,
  );
  const [linkTiers, setLinkTiers] = useSetting("linkTiers", ALL_LINK_TIERS);
  const [showTextReach, setShowTextReach] = useSetting("textReach", true);
  const [showCoverage, setShowCoverage] = useSetting("coverage", false);
  const [coverageMode, setCoverageMode] = useState<CoverageNodeMode>("heard");
  // The map's view and the area cells are fetched for, set when the map stops.
  const [coverageArea, setCoverageArea] = useState<CoverageArea | null>(null);
  const [selectedCell, setSelectedCell] = useState<CellAddress | null>(null);
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
    nodeId === null ? null : { nodeId, fit: "node" },
  );
  // The chosen link's key; it shows only while the filters keep the link.
  const [chosenLinkKey, setChosenLinkKey] = useState<string | null>(null);
  // A selected node the filters hide shows as nothing selected, and comes
  // back when they no longer hide it.
  const selectedNode =
    selection === null ? null : (nodeMap.get(selection.nodeId) ?? null);
  const unplacedSender = selection?.unplaced ?? null;
  const [hopDepth, setHopDepth] = useViewState("map.hopDepth", 0);
  // On a phone the sidebar is a sheet over the map: closed at first, and
  // opened by choosing something on the map.
  const [collapsed, setCollapsed] = useState(isNarrowScreen);
  const [mapMessage, setMapMessage] = useState<string | null>(null);
  const [basemap, setBasemap] = useSetting("basemap", BASEMAPS[0].id, (value) =>
    BASEMAPS.some((option) => option.id === value),
  );
  const shownBasemap = useRef(basemap);

  // The only ways the node selection changes.
  const select = useCallback((node: MapNode, fit: Selection["fit"]) => {
    setSelection({ nodeId: node.node_id, fit });
    setChosenLinkKey(null);
    setCoverageMode("heard");
    if (fit === "node" && map.current !== null) focusNode(map.current, node);
  }, []);
  const clearSelection = useCallback(() => {
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

  // With a node selected, only nodes within the hop depth over the links
  // switched on stay. Without any link layer there is nothing to walk, so
  // only the node itself stays (and the gateways of its texts, below).
  const anyLinkLayer = Object.values(linkChoice).some(Boolean);
  const focusNodeId = selectedNode?.node_id ?? null;
  const hopFocus = useMemo(
    () =>
      focusNodeId === null
        ? null
        : anyLinkLayer
          ? withinHops(focusNodeId, loadedLinks, hopDepth)
          : { nodeIds: [focusNodeId], linkKeys: [] },
    [focusNodeId, anyLinkLayer, loadedLinks, hopDepth],
  );

  // The selected node's text reach, for the same window as the map, unless
  // the text reach layer is off. It stays loaded while a filter hides the
  // node, but only shows while it is drawn.
  const reach = useApi<TextReach>(
    selection === null || !showTextReach
      ? null
      : textReachUrl(selection.nodeId, urls.since),
  );
  const reachShown = selectedNode !== null || unplacedSender !== null;
  const textReach = reachShown ? reach.data : null;
  const textReachError = reachShown ? reach.error : null;
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
            focus:
              unplacedSender !== null
                ? shares.length === 0
                  ? null
                  : new Set<number>()
                : hopFocus === null
                  ? null
                  : new Set(hopFocus.nodeIds),
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
    [selectedLink, unplacedSender, hopFocus, focusNodeId, shares],
  );

  // Coverage cells for the area in view, of the selected node unless "All
  // nodes" is chosen. The cells shown stay until the next ones arrive. They
  // ignore the time window: surveys are rare drives, kept for months.
  const coverageNode = coverageMode === "all" ? null : focusNodeId;
  // Measurements follow their sender's channel; "not reported" keeps them
  // all, as it does for links.
  const coverageChannel =
    nodeFilter.channel === ANY || nodeFilter.channel === NOT_REPORTED
      ? null
      : nodeFilter.channel;
  const cellsUrl = (view: CoverageView) =>
    coverageCellsUrl(view, coverageNode, coverageChannel);
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
      ? coverageCellUrl(selectedCell, coverageNode, coverageChannel)
      : null,
  );
  // The nodes heard in view, of every node whatever is selected. The last
  // answer stays until the next one arrives.
  const heardAnswer = useApi<CoverageNodeList>(
    showCoverage && coverageArea !== null
      ? coverageNodesUrl(coverageArea.view, coverageChannel)
      : null,
  );
  const [lastHeard, setLastHeard] = useState<CoverageNodeList | null>(null);
  if (heardAnswer.data !== null && heardAnswer.data !== lastHeard)
    setLastHeard(heardAnswer.data);
  const heardInView = heardAnswer.error === null ? lastHeard : null;
  // While coverage is on only the nodes heard in view and the selected one
  // are named, so names leave the cells visible; every node until the first
  // answer and after an error.
  const namedIds = useMemo(
    () =>
      showCoverage && heardInView !== null
        ? heardInView.nodes.map((node) => node.node_id)
        : null,
    [showCoverage, heardInView],
  );
  useEffect(() => {
    namedNodes.current = namedIds;
    if (map.current !== null) showNodeNames(map.current, namedIds);
  }, [namedIds]);
  const coverageCells = coverage.data?.truncated
    ? coverageInView.data
    : coverage.data;
  const coverageError = coverage.error ?? coverageInView.error;
  // Counts and the empty message are about what is in view, not the padding.
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
  useEffect(() => {
    if (showCoverage && coverageCells === null) return;
    const cells = buildCoverageFeatureCollection(
      showCoverage ? coverageCells : null,
    );
    coverageData.current.cells = cells;
    forgetCellHover.current();
    setSourceData(map.current, COVERAGE_SOURCE, cells);
  }, [showCoverage, coverageCells]);
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
      setSelection({ nodeId: wanted, fit: "node" });
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
      select(loaded, "node");
      return;
    }
    const controller = new AbortController();
    fetchMapNode(wanted, controller.signal)
      .then((linked) => {
        if (controller.signal.aborted) return;
        pendingNodeId.current = null;
        if (linked === null) return;
        setLinkedNode(linked);
        select(linked, "node");
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

  const { container: mapContainer, failure } = useBasemapMap(
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
    instance.addControl(new maplibregl.NavigationControl(), "bottom-left");
    // Prefetched nodes may be selected before the map exists.
    if (nodeId !== null && selectedNode?.node_id === nodeId)
      focusNode(instance, selectedNode);
    let styleLoaded = false;
    // True from the start of a style load, the first or a basemap switch,
    // until it finishes.
    let styleLoading = true;
    let hoveredId: string | number | null = null;
    let hoveredCellId: string | number | null = null;
    const nodeLayers = () => clickableNodeLayers(coverageShown.current);
    // Whether a click at the point would take a marker or a link line;
    // lines are taken from a few pixels away (nearestLine).
    const onNodeAt = (point: maplibregl.PointLike) =>
      instance.queryRenderedFeatures(point, {
        layers: [...nodeLayers(), "node-clusters"],
      }).length > 0;
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
      if (onNodeAt(event.point) || linkNear(event.point) !== undefined) {
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
      select(node, coverageShown.current ? "coverage" : "node");
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
    // A click on markers that overlap spreads them into a ring at high zoom,
    // and below it zooms in on them first.
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
      if (instance.getZoom() >= SPIDER_MIN_ZOOM) {
        openSpider(stacked);
        return;
      }
      const bounds = new maplibregl.LngLatBounds();
      for (const node of stacked)
        bounds.extend([node.longitude, node.latitude]);
      // Zooming may split them; the largest stack left is spread. Screen
      // distances work as soon as the zoom ends, before the markers redraw.
      const spreadIfStillStacked = () => {
        if (instance.getZoom() < SPIDER_MIN_ZOOM) return;
        const points = stacked.map((node) =>
          instance.project([node.longitude, node.latitude]),
        );
        let largest: MapNode[] = [];
        for (const point of points) {
          const still = stacked.filter(
            (_, index) => point.dist(points[index]) <= SPIDER_OVERLAP_PX,
          );
          if (still.length > largest.length) largest = still;
        }
        if (largest.length >= 2) openSpider(largest);
      };
      instance.once("moveend", spreadIfStillStacked);
      instance.easeTo({ center: bounds.getCenter(), zoom: SPIDER_MIN_ZOOM });
    };
    // A marker over a line wins; selectNode handles that click.
    const selectLink = (event: maplibregl.MapMouseEvent) => {
      if (onNodeAt(event.point)) return;
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
      const near = linkNear(event.point) !== undefined;
      if (near === linkHovered) return;
      linkHovered = near;
      if (near) instance.getCanvas().style.cursor = "pointer";
      else if (hoveredId === null) instance.getCanvas().style.cursor = "";
    };
    // MapLibre sends no click after a drag, so only a still click on empty map
    // clears the selection. With a link shown, it clears only the link, going
    // back to the node selected before.
    // A click on a coverage cell, and nothing above it, chooses the cell.
    const clearSelectionOnEmptyClick = (event: maplibregl.MapMouseEvent) => {
      if (onNodeAt(event.point) || linkNear(event.point) !== undefined) return;
      const cell = instance.queryRenderedFeatures(event.point, {
        layers: [COVERAGE_FILL_LAYER],
      })[0]?.properties;
      if (cell === undefined) {
        if (selectedLinkKey.current !== null) setChosenLinkKey(null);
        else clearSelection();
        setSelectedCell(null);
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
      showNodeNames(instance, namedNodes.current);
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

  // A sender chosen from Chatty nodes is fitted with its drawn gateways once
  // its text reach arrives, or on its own if that fails or the text
  // reach layer is off. A link chosen meanwhile keeps the camera where it is.
  const reachLoaded = textReach !== null;
  const reachFailed = textReachError !== null;
  const fitTextReach = useEffectEvent(() => {
    const instance = map.current;
    if (
      instance === null ||
      (selectedNode === null && unplacedSender === null) ||
      selectedLink !== null
    )
      return;
    const gateways = (textReach?.gateways ?? []).flatMap((gateway) => {
      const node = nodeMap.get(gateway.node_id);
      return node === undefined ? [] : [node];
    });
    focusTextReach(instance, selectedNode, gateways);
  });
  useEffect(() => {
    if (
      selection?.fit === "reach" &&
      (reachLoaded || reachFailed || !showTextReach)
    )
      fitTextReach();
  }, [selection, reachLoaded, reachFailed, showTextReach]);

  // A node chosen with coverage on is shown with its cells once their extent
  // arrives, or centred if it has none or that fails. Each choice fits once,
  // not again when the channel changes.
  const extentUrl =
    selection?.fit === "coverage" && showCoverage
      ? coverageExtentUrl(selection.nodeId, coverageChannel)
      : null;
  const extent = useApi<CoverageExtent | null>(extentUrl);
  const extentDone = extentUrl !== null && !extent.loading;
  const coverageFitted = useRef<Selection | null>(null);
  const fitCoverage = useEffectEvent(() => {
    const instance = map.current;
    if (instance === null || selectedNode === null || selectedLink !== null)
      return;
    if (extent.data === null) focusNode(instance, selectedNode);
    else
      focusCoverage(
        instance,
        selectedNode,
        extent.data,
        selection?.zoomIn ?? false,
      );
  });
  useEffect(() => {
    if (!extentDone || coverageFitted.current === selection) return;
    coverageFitted.current = selection;
    fitCoverage();
  }, [selection, extentDone]);

  // Choosing a chatty node is asking for its text reach.
  function chooseTextSender(sender: TextSender) {
    setShowTextReach(true);
    const node = nodeMap.get(sender.node_id);
    if (node !== undefined) {
      select(node, "reach");
      return;
    }
    setSelection({ nodeId: sender.node_id, fit: "reach", unplaced: sender });
    setChosenLinkKey(null);
  }

  // Selections are cleared, as the reloaded nodes and links may no longer
  // include them or may have different details.
  function chooseSeenWindow(window: SeenWindow) {
    clearSelection();
    setSelectedCell(null);
    setLinkedNode(null);
    setRequestedId(null);
    setSeenWindow(window);
    setShownWindow(mapWindowStart(window));
  }

  function toggleLinkLayer(linkKind: LinkKind, shown: boolean) {
    setLinkChoice({ ...linkChoice, [linkKind]: shown });
  }

  function toggleCoverage(shown: boolean) {
    setShowCoverage(shown);
    if (!shown) setSelectedCell(null);
  }

  // A node chosen in the cell card shows where it was heard, without moving
  // the map away from the cell.
  function chooseHeardNode(nodeId: number) {
    setSelection({ nodeId, fit: "node" });
    setChosenLinkKey(null);
    setCoverageMode("heard");
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
          {showCoverage && coverageCellsInView?.cells.length === 0 ? (
            <StatusPanel>
              No coverage measurements in view
              {coverageNode === null ? "" : " for the selected node"}.
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
        />
        <BasemapSwitcher value={basemap} onChange={setBasemap} />
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
          className="sidebar-scroll"
          hidden={collapsed}
        >
          {selectedLinkNodes !== null ? (
            <section
              className="selected-node sidebar-selection"
              aria-live="polite"
            >
              <MapLinkDetails
                linkKind={selectedLinkNodes.linkKind}
                link={selectedLinkNodes.link}
                nodeA={selectedLinkNodes.a}
                nodeB={selectedLinkNodes.b}
                km={
                  // Shown between reported positions only: a link to an
                  // estimate mostly measures the estimate.
                  selectedLinkNodes.a.estimate === null &&
                  selectedLinkNodes.b.estimate === null
                    ? drawnKm(selectedLinkNodes.a, selectedLinkNodes.b)
                    : null
                }
                since={urls.since}
                onClose={() => setChosenLinkKey(null)}
              />
            </section>
          ) : null}
          {unplacedSender !== null && selectedLinkNodes === null ? (
            <UnplacedSenderCard
              sender={unplacedSender}
              reach={textReach}
              reachError={textReachError}
              showTextReach={showTextReach}
              onClose={clearSelection}
            />
          ) : null}
          <section
            className="selected-node sidebar-selection"
            aria-live="polite"
            hidden={selectedLinkNodes !== null || selectedNode === null}
          >
            {selectedNode === null ? null : (
              <>
                <div className="selected-node-heading">
                  <span className="node-avatar avatar-green" title="Short name">
                    {nodeMarkerLabel(selectedNode)}
                  </span>
                  <div>
                    <h3>{nodeLabel(selectedNode)}</h3>
                    <span className="muted">
                      <CopyValue
                        value={formatNodeId(selectedNode.node_id)}
                        label="node ID"
                      />
                    </span>
                  </div>
                  <a
                    className="icon-button"
                    href={nodeHref(selectedNode.node_id)}
                    aria-label="View node details"
                    title="View node details"
                  >
                    <Icon name="open" />
                  </a>
                  <IconButton
                    icon="close"
                    label="Clear selected node"
                    onClick={clearSelection}
                  />
                </div>
                <div className="hop-depth">
                  <label className="section-label" htmlFor="map-hop-depth">
                    Hop depth
                  </label>
                  <input
                    id="map-hop-depth"
                    type="range"
                    min={0}
                    max={MAX_HOP_DEPTH}
                    step={1}
                    value={hopDepth}
                    disabled={!anyLinkLayer}
                    onChange={(event) =>
                      setHopDepth(Number(event.target.value))
                    }
                  />
                  <output htmlFor="map-hop-depth">{hopDepth}</output>
                  <span className="hop-depth-note">
                    {anyLinkLayer
                      ? `${nearbyShown.toLocaleString()} ${nearbyShown === 1 ? "node" : "nodes"} ${
                          hopDepth === 0
                            ? "linked directly"
                            : `within ${hopDepth} ${hopDepth === 1 ? "relay" : "relays"}`
                        }`
                      : "Switch on a link layer to use hop depth."}
                  </span>
                </div>
                {textReachError !== null ? (
                  <p className="sidebar-note">
                    Text reach could not load. {textReachError}
                  </p>
                ) : null}
                <dl className="node-summary">
                  <div>
                    <dt>Role</dt>
                    <dd className={selectedNode.role === null ? "muted" : ""}>
                      {formatRole(selectedNode.role)}
                      {selectedNode.last_gateway_at == null ? null : (
                        <span
                          className="gateway-note"
                          title={`Last reported a packet ${new Date(selectedNode.last_gateway_at).toLocaleString()}`}
                        >
                          (Gateway)
                        </span>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>Channel</dt>
                    <dd
                      className={selectedNode.channel === null ? "muted" : ""}
                    >
                      {selectedNode.channel ?? "Unknown"}
                    </dd>
                  </div>
                  <div>
                    <dt>Altitude</dt>
                    {selectedNode.position?.altitude_meters == null ? (
                      <dd className="muted">Unknown</dd>
                    ) : (
                      <dd>{selectedNode.position.altitude_meters} m</dd>
                    )}
                  </div>
                  <div>
                    <dt>Last seen</dt>
                    <dd>
                      <ShortTime value={selectedNode.last_seen_at} />
                    </dd>
                  </div>
                  <div>
                    <dt>Last sent</dt>
                    {selectedNode.last_sent_at === null ? (
                      <dd className="muted">No own packet observed</dd>
                    ) : (
                      <dd>
                        <ShortTime value={selectedNode.last_sent_at} />
                        {selectedNode.last_sent_packet_row_id ===
                        null ? null : (
                          <a
                            className="text-button"
                            href={`#packets/${selectedNode.last_sent_packet_row_id}`}
                          >
                            Latest packet
                          </a>
                        )}
                      </dd>
                    )}
                  </div>
                  {selectedNode.estimate !== null ? (
                    <div>
                      <dt>Position</dt>
                      <dd>{estimateText(selectedNode.estimate)}</dd>
                    </div>
                  ) : selectedNode.position?.from_map_report ? (
                    <div>
                      <dt>Position</dt>
                      <dd>
                        {mapReportPositionText(
                          selectedNode.position.precision_km,
                        )}
                      </dd>
                    </div>
                  ) : null}
                </dl>
                {showCoverage ? (
                  <CoverageSwitch
                    mode={coverageMode}
                    onMode={setCoverageMode}
                    cells={coverageMode === "all" ? null : coverageCellsInView}
                    onFit={() =>
                      setSelection({
                        nodeId: selectedNode.node_id,
                        fit: "coverage",
                        zoomIn: true,
                      })
                    }
                  />
                ) : null}
              </>
            )}
          </section>
          {showCoverage && selectedCell !== null ? (
            <CoverageCellDetails
              detail={cellDetail}
              onlyOf={
                selectedNode === null || coverageNode === null
                  ? null
                  : `Only where ${nodeLabel(selectedNode)} was heard`
              }
              isDrawn={(nodeId) => nodeMap.has(nodeId)}
              onChooseNode={chooseHeardNode}
              onClose={() => setSelectedCell(null)}
            />
          ) : null}
          <NodeSearch
            nodes={items}
            onSelect={(node) =>
              select(node, showCoverage ? "coverage" : "node")
            }
            keepResults
          />
          {showCoverage ? (
            <HeardInView
              page={heardInView}
              error={heardAnswer.error}
              selectedNodeId={focusNodeId}
              drawn={nodeMap}
              loadedIds={loadedIds}
              onChoose={(node) => select(node, "coverage")}
            />
          ) : null}
          <ChattyNodes
            page={shownTextSenders}
            error={textSenders.error}
            selectedNodeId={focusNodeId ?? unplacedSender?.node_id ?? null}
            placedIds={loadedIds}
            onChoose={chooseTextSender}
          />
        </div>
      </aside>
    </section>
  );
}

// A chatty node with no recent position: its gateways show on the map, not it.
function UnplacedSenderCard({
  sender,
  reach,
  reachError,
  showTextReach,
  onClose,
}: {
  sender: TextSender;
  reach: TextReach | null;
  reachError: string | null;
  showTextReach: boolean;
  onClose: () => void;
}) {
  return (
    <section className="selected-node sidebar-selection" aria-live="polite">
      <div className="selected-node-heading">
        <span className="node-avatar avatar-green" title="Short name">
          {shortLabel(sender.node_id, sender.short_name)}
        </span>
        <div>
          <h3>{sender.long_name ?? formatNodeId(sender.node_id)}</h3>
          <span className="muted">
            <CopyValue value={formatNodeId(sender.node_id)} label="node ID" />
          </span>
        </div>
        <a
          className="icon-button"
          href={nodeHref(sender.node_id)}
          aria-label="View node details"
          title="View node details"
        >
          <Icon name="open" />
        </a>
        <IconButton
          icon="close"
          label="Clear selected node"
          onClick={onClose}
        />
      </div>
      <p className="sidebar-note">
        No recent position, so it is not on the map.{" "}
        {!showTextReach
          ? "Switch on Text reach to see its gateways."
          : reachError !== null
            ? `Text reach could not load. ${reachError}`
            : reach === null
              ? "Loading its gateways…"
              : reach.gateways.length === 0
                ? "No gateway reported its texts."
                : `Showing the gateways that reported its ${reach.messages.toLocaleString()} ${reach.messages === 1 ? "text" : "texts"} on ${reach.channel}.`}
      </p>
    </section>
  );
}

const COVERAGE_MODES: {
  mode: CoverageNodeMode;
  label: string;
  title: string;
}[] = [
  {
    mode: "heard",
    label: "Heard from it",
    title: "Where survey nodes heard this node",
  },
  { mode: "all", label: "All nodes", title: "Coverage of every node" },
];

// What the coverage layer shows while a node is selected, and how much of it
// is guessed in the area in view.
function CoverageSwitch({
  mode,
  onMode,
  cells,
  onFit,
}: {
  mode: CoverageNodeMode;
  onMode: (mode: CoverageNodeMode) => void;
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
    <div className="coverage-switch">
      <p className="section-label" id="map-coverage-mode">
        Coverage
      </p>
      <div role="radiogroup" aria-labelledby="map-coverage-mode">
        {COVERAGE_MODES.map((option) => (
          <label key={option.mode} title={option.title}>
            <input
              type="radio"
              name="map-coverage-mode"
              checked={mode === option.mode}
              onChange={() => onMode(option.mode)}
            />
            {option.label}
          </label>
        ))}
      </div>
      {cells === null ? null : (
        <p className="sidebar-note">
          {measurements === 0
            ? "No measurements in view."
            : `${guessed.toLocaleString()} of ${measurements.toLocaleString()} measurements in view identified by guess.`}
        </p>
      )}
      <button type="button" className="text-button" onClick={onFit}>
        Fit map to coverage
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
