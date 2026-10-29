import type { components } from "./api.generated";

// Response shapes are generated from the backend's response classes by
// scripts/api-types.sh; their docstrings explain the fields.
type Schemas = components["schemas"];

export type PacketDetail = Schemas["PacketDetail"];
export type PacketReception = Schemas["PacketReception"];
export type FloodRoutes = Schemas["FloodRoutes"];
export type RoutePath = Schemas["RoutePath"];
export type ReportPaths = Schemas["ReportPaths"];
export type ReplyRoute = Schemas["ReplyRoute"];
export type TracerouteHop = Schemas["TracerouteHop"];
export type NodeSummary = Schemas["NodeSummary"];
export type NodePosition = Schemas["NodePosition"];
export type MapNode = Schemas["MapNode"];
/** A map node with a reported position, which line of sight needs. */
export type ReportedNode = MapNode & { position: NodePosition };
export type EstimatedPosition = Schemas["EstimatedPosition"];

export type FirmwareEstimate = Schemas["FirmwareEstimate"];
export type NodeDetail = Schemas["NodeDetail"];
export type PacketRow = Schemas["PacketRow"];
export type NodeOverview = Schemas["NodeOverview"];
export type PacketPage = Schemas["PacketPage"];
export type PacketPort = Schemas["PacketPort"];
export type PacketPortList = Schemas["PacketPortList"];
export type MapNodeList = Schemas["MapNodeList"];
export type LinkDirection = Schemas["LinkDirection"];
export type MapLink = Schemas["MapLink"];
export type MapLinkList = Schemas["MapLinkList"];
export type TextSender = Schemas["TextSender"];
export type TextSenderList = Schemas["TextSenderList"];
export type TextReachGateway = Schemas["TextReachGateway"];
export type TextReach = Schemas["TextReach"];
export type DiagnosticsSummary = Schemas["DiagnosticsSummary"];
export type LongLink = Schemas["LongLink"];
export type LongLinkEnd = Schemas["LongLinkEnd"];
export type LongLinkList = Schemas["LongLinkList"];
export type DoubtfulNode = Schemas["DoubtfulNode"];
export type DoubtfulNodeList = Schemas["DoubtfulNodeList"];
export type DeviceConflictList = Schemas["DeviceConflictList"];
export type RelayCheck = Schemas["RelayCheck"];
export type RelayCheckList = Schemas["RelayCheckList"];
export type RelayCheckDetail = Schemas["RelayCheckDetail"];
export type RelayCandidate = Schemas["RelayCandidate"];
export type RelayCheckReport = Schemas["RelayCheckReport"];
export type RelayAnswer = Schemas["RelayAnswer"];
export type SurveyCheck = Schemas["SurveyCheck"];
export type SurveyCheckList = Schemas["SurveyCheckList"];
export type SurveyCheckDetail = Schemas["SurveyCheckDetail"];
export type SurveyLinkNode = Schemas["SurveyLinkNode"];
export type ChatParent = Schemas["ChatParent"];
export type ChatMessage = Schemas["ChatMessage"];
export type ChatPage = Schemas["ChatPage"];
export type NodeListItem = Schemas["NodeListItem"];
export type NodeListPage = Schemas["NodeListPage"];
export type NodeChannelList = Schemas["NodeChannelList"];
export type ChatChannelList = Schemas["ChatChannelList"];
export type NodeChartsHour = Schemas["NodeChartsHour"];
export type NodeUtilization = Schemas["NodeUtilization"];
export type NodeCharts = Schemas["NodeCharts"];
export type NodePositionHistory = Schemas["NodePositionHistory"];
export type NodeContactList = Schemas["NodeContactList"];
export type NodeContact = Schemas["NodeContact"];
export type ContactLink = Schemas["ContactLink"];
export type LinkRow = Schemas["LinkRow"];
export type LinkTable = Schemas["LinkTable"];
export type ZeroCostHop = Schemas["ZeroCostHop"];
export type HeardPositionList = Schemas["HeardPositionList"];
export type TracerouteRoute = Schemas["TracerouteRoute"];
export type TracerouteRun = Schemas["TracerouteRun"];
export type TracerouteRuns = Schemas["TracerouteRuns"];
export type NodeCount = Schemas["NodeCount"];
export type BusiestTraceroutes = Schemas["BusiestTraceroutes"];
export type HopObservation = Schemas["HopObservation"];
export type HopObservations = Schemas["HopObservations"];
export type HopCost = Schemas["HopCost"];
export type RouteHopCost = Schemas["RouteHopCost"];
export type LinkHours = Schemas["LinkHours"];
export type HeardHour = Schemas["HeardHour"];
export type Zone = Schemas["Zone"];
export type ZonePairLink = Schemas["ZonePairLink"];
export type ZonePair = Schemas["ZonePair"];
export type ZoneTextReach = Schemas["ZoneTextReach"];
export type KeyRelay = Schemas["KeyRelay"];
export type ZoneRules = Schemas["ZoneRules"];
export type NetworkZones = Schemas["NetworkZones"];
export type CoverageCell = Schemas["CoverageCell"];
export type CoverageCellList = Schemas["CoverageCellList"];
export type CoverageExtent = Schemas["CoverageExtent"];
export type CoverageNodeList = Schemas["CoverageNodeList"];
export type CoverageViewNode = Schemas["CoverageViewNode"];
export type CoverageCellDetail = Schemas["CoverageCellDetail"];
export type CoverageCellNetwork = Schemas["CoverageCellNetwork"];
export type CoverageHeardNode = Schemas["CoverageHeardNode"];
export type CoverageMeasurementRow = Schemas["CoverageMeasurementRow"];

// Matches the backend default, so the truncation notice names the real cap.
export const MAP_NODE_LIMIT = 10_000;

/**
 * Where a link comes from: traceroute route steps, a gateway hearing the
 * sender with no relay between, or neighbour info reported by a node.
 * The kinds are never combined.
 */
export type LinkKind = "traceroute" | "direct" | "neighbor";

export const MAP_LINK_LIMIT = 20_000;

/** Cutoffs as ISO 8601 times; undefined leaves that cutoff out. */
export interface MapRequest {
  seenSince?: string;
  positionSince?: string;
}

export type NodeSort =
  | "last_sent"
  | "traceroute"
  | "packets"
  | "texts"
  | "channel_utilization"
  | "air_util_tx"
  | "firmware"
  | "firmware_oldest"
  | "altitude"
  | "altitude_lowest";

export interface NodeListRequest {
  query: string;
  channel: string | null;
  gatewayOnly: boolean;
  sort: NodeSort;
  offset: number;
}

export const NODE_PAGE_SIZE = 20;

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
  };
}

/**
 * Which saved packets to list: on one application port, sent by one node,
 * and heard with no relay by one gateway (which needs a sender). Null is any.
 */
export interface PacketFilter {
  port: number | null;
  sender: number | null;
  heardDirectlyBy: number | null;
}

export function packetsUrl(filter: PacketFilter): string {
  const query = new URLSearchParams();
  if (filter.port !== null) query.set("port", String(filter.port));
  if (filter.sender !== null) {
    query.set("sender", String(filter.sender));
    if (filter.heardDirectlyBy !== null) {
      query.set("heard_directly_by", String(filter.heardDirectlyBy));
    }
  }
  const search = query.toString();
  return search === "" ? "/api/v1/packets" : `/api/v1/packets?${search}`;
}

export const packetPortsUrl = "/api/v1/packets/ports";

export function packetUrl(packetRowId: number): string {
  return `/api/v1/packets/${packetRowId}`;
}

export function floodRoutesUrl(packetRowId: number): string {
  return `/api/v1/packets/${packetRowId}/flood-routes`;
}

export function nodeUrl(nodeId: number): string {
  return `/api/v1/nodes/${nodeId}`;
}

/** The node's packets, only those on application port `port` when it is not null. */
export function nodePacketsUrl(nodeId: number, port: number | null): string {
  const url = `/api/v1/nodes/${nodeId}/packets`;
  return port === null ? url : `${url}?port=${port}`;
}

export function nodeChartsUrl(nodeId: number, since: Date): string {
  const query = new URLSearchParams({ since: since.toISOString() });
  return `/api/v1/nodes/${nodeId}/charts?${query}`;
}

export function nodePositionsUrl(nodeId: number): string {
  return `/api/v1/nodes/${nodeId}/positions`;
}

export function nodeContactsUrl(nodeId: number, since: Date): string {
  const query = new URLSearchParams({ since: since.toISOString() });
  return `/api/v1/nodes/${nodeId}/contacts?${query}`;
}

export function nodeLinksUrl(nodeId: number, since: Date): string {
  const query = new URLSearchParams({ since: since.toISOString() });
  return `/api/v1/nodes/${nodeId}/links?${query}`;
}

export function nodesUrl(request: NodeListRequest): string {
  const query = new URLSearchParams({
    sort: request.sort,
    limit: String(NODE_PAGE_SIZE),
    offset: String(request.offset),
  });
  setQuery(query, "q", request.query.trim());
  if (request.channel !== null) query.set("channel", request.channel);
  if (request.gatewayOnly) query.set("gateway", "true");
  return `/api/v1/nodes?${query}`;
}

export const nodeChannelsUrl = "/api/v1/nodes/channels";

export function mapNodesUrl(mapRequest: MapRequest): string {
  const query = new URLSearchParams({ limit: String(MAP_NODE_LIMIT) });
  setQuery(query, "seen_since", mapRequest.seenSince);
  setQuery(query, "position_since", mapRequest.positionSince);
  return `/api/v1/map/nodes?${query}`;
}

export function mapLinksUrl(linkKind: LinkKind, since?: string): string {
  const query = new URLSearchParams({
    link_kind: linkKind,
    limit: String(MAP_LINK_LIMIT),
  });
  setQuery(query, "since", since);
  return `/api/v1/map/links?${query}`;
}

export function textSendersUrl(since?: string): string {
  const query = new URLSearchParams();
  setQuery(query, "since", since);
  const search = query.toString();
  return search === ""
    ? "/api/v1/map/text-senders"
    : `/api/v1/map/text-senders?${search}`;
}

export function textReachUrl(nodeId: number, since?: string): string {
  const query = new URLSearchParams();
  setQuery(query, "since", since);
  const search = query.toString();
  const url = `/api/v1/map/text-reach/${nodeId}`;
  return search === "" ? url : `${url}?${search}`;
}

/** What the Chat page shows: one channel or all, a search, senders hidden. */
export type ChatFilter = {
  channel: string | null;
  search: string;
  hidden: number[];
};

export function chatUrl(filter: ChatFilter, afterId?: number): string {
  const query = new URLSearchParams();
  if (filter.channel !== null) query.set("channel", filter.channel);
  setQuery(query, "search", filter.search.trim());
  for (const nodeId of filter.hidden) query.append("hide", String(nodeId));
  if (afterId !== undefined) query.set("after_id", String(afterId));
  const search = query.toString();
  return search === "" ? "/api/v1/chat" : `/api/v1/chat?${search}`;
}

export const chatChannelsUrl = "/api/v1/chat/channels";

export const diagnosticsSummaryUrl = "/api/v1/diagnostics/summary";

/** What a page of long links keeps; see LongLinkFilter in diagnostics.py. */
export type LongLinkFilter =
  | "long"
  | "shorter"
  | "hidden"
  | "position"
  | "unlikely"
  | "pending"
  | "checks";

/** How a page of long links is ranked; see LongLinkOrder in diagnostics.py. */
export type LongLinkOrder = "longest" | "latest";

export function longestLinksUrl(request: {
  offset: number;
  count: number;
  only: LongLinkFilter | null;
  node: number | null;
  order: LongLinkOrder;
}): string {
  const query = new URLSearchParams({
    offset: String(request.offset),
    count: String(request.count),
  });
  if (request.only !== null) query.set("only", request.only);
  if (request.node !== null) query.set("node", String(request.node));
  if (request.order !== "longest") query.set("order", request.order);
  return `/api/v1/diagnostics/longest-links?${query}`;
}

export const doubtfulNodesUrl = "/api/v1/diagnostics/doubtful-nodes";

export function deviceConflictsUrl(afterNode = 0): string {
  return `/api/v1/diagnostics/device-conflicts?after_node=${afterNode}&count=30`;
}

/** The relay checks of the `count` newest traceroute packets. */
export function relayChecksUrl(count: number): string {
  return `/api/v1/diagnostics/relay-checks?count=${count}`;
}

/** One reception's relay check, with its receiver moved to `at` when given. */
export function relayCheckUrl(
  receptionId: number,
  at: [number, number] | null = null,
  receiver: "survey" | "gateway" = "survey",
): string {
  const url = `/api/v1/diagnostics/relay-checks/${receptionId}`;
  if (receiver === "gateway") return `${url}?receiver=gateway`;
  return at === null
    ? url
    : `${url}?latitude=${at[0].toFixed(5)}&longitude=${at[1].toFixed(5)}`;
}

/** The `count` newest heard survey measurements, relayed ones unless `direct`. */
export function surveyChecksUrl(count: number, direct: boolean): string {
  return `/api/v1/diagnostics/survey-checks?count=${count}${direct ? "&direct=true" : ""}`;
}

export function surveyCheckUrl(measurementId: number): string {
  return `/api/v1/diagnostics/survey-checks/${measurementId}`;
}

function setQuery(query: URLSearchParams, key: string, value?: string) {
  if (value !== undefined && value !== "") {
    query.set(key, value);
  }
}

export async function request<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    let errorBody: ErrorEnvelope = {};
    try {
      errorBody = (await response.json()) as ErrorEnvelope;
    } catch {
      // A proxy or server may return a non-JSON failure. Use the safe fallback below.
    }
    throw new Error(
      errorBody.error?.message ?? "SigSleuth could not load this history.",
    );
  }
  return (await response.json()) as T;
}

/** route: longest route first; distance: ends farthest apart first. */
export type TracerouteSort = "newest" | "route" | "distance";

export interface TracerouteFilters {
  packet?: number | null;
  since: string | undefined;
  sort: TracerouteSort;
  /** A node search: name, !hex or number. */
  node: string | null;
  /** A second node search: runs then need a hop between a node of each. */
  nextTo: string | null;
  channel: string | null;
  minHops: number;
  /** Only runs, and routes, known to have reached their end. */
  successful: boolean;
  offset: number;
}

export const TRACEROUTE_PAGE_SIZE = 50;

export function linkTableUrl(linkKind: LinkKind, since?: string): string {
  const query = new URLSearchParams({ link_kind: linkKind });
  setQuery(query, "since", since);
  return `/api/v1/links?${query}`;
}

/** Where two nodes were at a time in ms, by their own positions. */
export function linkPositionsUrl(
  nodeA: number,
  nodeB: number,
  at: number,
): string {
  const query = new URLSearchParams({
    node_a: String(nodeA),
    node_b: String(nodeB),
    at: new Date(at).toISOString(),
  });
  return `/api/v1/links/positions?${query}`;
}

export function linkHoursUrl(
  linkKind: LinkKind,
  nodeA: number,
  nodeB: number,
  channel: string,
  since?: string,
): string {
  const query = new URLSearchParams({
    link_kind: linkKind,
    node_a: String(nodeA),
    node_b: String(nodeB),
    channel,
  });
  setQuery(query, "since", since);
  return `/api/v1/links/hours?${query}`;
}

export function traceroutesUrl(filters: TracerouteFilters): string {
  const query = new URLSearchParams({
    sort: filters.sort,
    limit: String(TRACEROUTE_PAGE_SIZE),
    offset: String(filters.offset),
  });
  setQuery(query, "since", filters.since);
  if (filters.node !== null) query.set("node", filters.node);
  if (filters.nextTo !== null) query.set("next_to", filters.nextTo);
  if (filters.channel !== null) query.set("channel", filters.channel);
  if (filters.minHops > 0) query.set("min_hops", String(filters.minHops));
  if (filters.successful) query.set("successful", "true");
  if (filters.packet != null) query.set("packet", String(filters.packet));
  return `/api/v1/traceroutes?${query}`;
}

/** The packets that reported a hop between two nodes, on channel when given. */
export function tracerouteHopsUrl(
  nodeA: number,
  nodeB: number,
  since?: string,
  channel?: string,
): string {
  const query = new URLSearchParams({
    node_a: String(nodeA),
    node_b: String(nodeB),
  });
  setQuery(query, "since", since);
  setQuery(query, "channel", channel);
  return `/api/v1/traceroutes/hops?${query}`;
}

export function busiestTraceroutesUrl(since?: string): string {
  const query = new URLSearchParams();
  setQuery(query, "since", since);
  const search = query.toString();
  return search === ""
    ? "/api/v1/traceroutes/busiest"
    : `/api/v1/traceroutes/busiest?${search}`;
}

export function hasReportedPosition(node: MapNode): node is ReportedNode {
  return node.position !== null;
}

/**
 * A node fetched on its own, drawn at its reported position, else at its
 * estimate; null when neither places it.
 */
export async function fetchMapNode(
  nodeId: number,
  signal: AbortSignal,
): Promise<MapNode | null> {
  const { node } = await request<NodeOverview>(nodeUrl(nodeId), signal);
  const drawnAt = node?.position ?? node?.estimate ?? null;
  if (node === null || drawnAt === null) return null;
  return {
    ...node,
    latitude: drawnAt.latitude,
    longitude: drawnAt.longitude,
  };
}

/** The Zones page's windows, in hours. */
export const ZONE_WINDOWS = [24, 72, 168] as const;
export type ZoneWindow = (typeof ZONE_WINDOWS)[number];

export interface NetworkZonesRequest {
  windowHours: ZoneWindow;
  /** Null asks for the channel with the most links. */
  channel: string | null;
  /** The rule for solid links: the quieter direction heard this often a day, and both directions this far above the floor. */
  perDay: number;
  minMarginDb: number;
}

export function networkZonesUrl(request: NetworkZonesRequest): string {
  const query = new URLSearchParams({
    window_hours: String(request.windowHours),
    per_day: String(request.perDay),
    min_margin_db: String(request.minMarginDb),
  });
  if (request.channel !== null) query.set("channel", request.channel);
  return `/api/v1/zones?${query}`;
}
