import type { LinkKind } from "./api";
import type { LinksTab } from "./LinksView";
import {
  DEFAULT_SEEN_WINDOW,
  type SeenWindow,
  isSeenWindow,
} from "./mapTimeFilter";
import type { DirectedHop } from "./minimapMarkers";
import { formatNodeId } from "./nodeDisplay";

export type Route =
  | { page: "map"; nodeId: number | null; view: MapLinkView | null }
  | { page: "zones" }
  | { page: "chat" }
  | { page: "nodes" }
  | {
      page: "links";
      tab: LinksTab;
      pair: [number, number] | null;
      node: number | null;
      at: number | null;
      /** On the links tab, the kind a link to one link's row names. */
      kind: LinkKind | null;
    }
  | {
      page: "diagnostics";
      sender: number | null;
      heardDirectlyBy: number | null;
    }
  | { page: "packet"; id: number; hop: DirectedHop | null; relays: boolean }
  | { page: "node"; id: number; view: NodeLinkView | null };

/** What the map shows of a selected node besides the node itself. */
export type MapLayer = LinkKind | "reach" | "coverage";
const MAP_LAYERS: readonly MapLayer[] = [
  "traceroute",
  "direct",
  "neighbor",
  "reach",
  "coverage",
];

/**
 * A shared map view of one node. It applies to the history entry the link
 * opens, never to the saved settings: a layer is on only when `show` lists
 * it, and the nodes are filtered by `channel` alone.
 */
export interface MapLinkView {
  show: MapLayer[];
  window: SeenWindow;
  /** The node filter's channel; null for every channel. */
  channel: string | null;
}

/** What a node page's position minimap shows besides the positions. */
export type MinimapLayer = "traceroutes" | "gateways" | "coverage";
const MINIMAP_LAYERS: readonly MinimapLayer[] = [
  "traceroutes",
  "gateways",
  "coverage",
];

/** A shared node page: a layer is on only when `show` lists it. */
export interface NodeLinkView {
  expanded: boolean;
  show: MinimapLayer[];
}

// A node number, or its !hex form as Meshtastic writes it (not !00000000).
const NODE = String.raw`([1-9]\d*|!(?=[0-9a-fA-F]*[1-9a-fA-F])[0-9a-fA-F]{1,8})`;

function nodeNumber(text: string): number {
  return text.startsWith("!") ? parseInt(text.slice(1), 16) : Number(text);
}

export function routeFromHash(hash: string): Route {
  // #health is the page's old name.
  if (hash === "#zones" || hash === "#health") {
    return { page: "zones" };
  }
  if (hash === "#chat") {
    return { page: "chat" };
  }
  if (hash === "#nodes") {
    return { page: "nodes" };
  }
  // The links or the explorer, optionally around one node; the explorer or
  // the links, optionally one link's two nodes; or the line of sight,
  // optionally between two nodes, where they were at a time in ms. On the
  // links, ?kind= names the kind of the link whose row opens.
  const linksTab = hash.match(
    new RegExp(
      String.raw`^#links(?:\/(traceroutes|line-of-sight))?(?:\/${NODE}(?:-${NODE})?)?(?:@(\d+))?(?:\?kind=(traceroute|direct|neighbor))?$`,
    ),
  );
  if (linksTab !== null) {
    const [, tab = "links", first, second, time, kind] = linksTab;
    const pair: [number, number] | null =
      second === undefined ? null : [nodeNumber(first), nodeNumber(second)];
    const node =
      first === undefined || second !== undefined || tab === "line-of-sight"
        ? null
        : nodeNumber(first);
    const at =
      time === undefined || pair === null || tab !== "line-of-sight"
        ? null
        : Number(time);
    return {
      page: "links",
      tab: tab as LinksTab,
      pair,
      node,
      at,
      kind: kind === undefined ? null : (kind as LinkKind),
    };
  }
  // Diagnostics, optionally listing the packets one node sent, optionally
  // only those a second heard directly.
  const diagnostics = hash.match(
    new RegExp(String.raw`^#diagnostics(?:\/packets\/${NODE}(?:-${NODE})?)?$`),
  );
  if (diagnostics !== null) {
    const [, first, second] = diagnostics;
    const ids = [first, second].map((id) =>
      id === undefined || nodeNumber(id) > 0xffffffff ? null : nodeNumber(id),
    );
    return {
      page: "diagnostics",
      sender: ids[0],
      heardDirectlyBy: ids[0] !== null ? ids[1] : null,
    };
  }
  // A packet, optionally showing only the reports where the first node
  // was heard by the second, or showing its relays.
  const packet = hash.match(
    new RegExp(
      String.raw`^#packets\/([1-9]\d*)(?:\/hop\/${NODE}-${NODE}|\/(relays))?$`,
    ),
  );
  if (packet !== null && Number.isSafeInteger(Number(packet[1]))) {
    const hop =
      packet[2] === undefined
        ? null
        : {
            fromNodeId: nodeNumber(packet[2]),
            toNodeId: nodeNumber(packet[3]),
          };
    return {
      page: "packet",
      id: Number(packet[1]),
      hop,
      relays: packet[4] !== undefined,
    };
  }
  const node = hash.match(new RegExp(String.raw`^#nodes\/${NODE}(?:\?(.*))?$`));
  if (node !== null && nodeNumber(node[1]) <= 0xffffffff) {
    return {
      page: "node",
      id: nodeNumber(node[1]),
      view: nodeLinkView(new URLSearchParams(node[2])),
    };
  }
  const mapNode = hash.match(
    new RegExp(String.raw`^#map\/${NODE}(?:\?(.*))?$`),
  );
  if (mapNode !== null && nodeNumber(mapNode[1]) <= 0xffffffff) {
    return {
      page: "map",
      nodeId: nodeNumber(mapNode[1]),
      view: mapLinkView(new URLSearchParams(mapNode[2])),
    };
  }
  return { page: "map", nodeId: null, view: null };
}

/** The listed names among `known`, in their order; unknown names are left out. */
function listed<T extends string>(
  query: URLSearchParams,
  known: readonly T[],
): T[] {
  const names = (query.get("show") ?? "").split(",");
  return known.filter((name) => names.includes(name));
}

/** A map link's view; null when it names nothing but the node. */
function mapLinkView(query: URLSearchParams): MapLinkView | null {
  if (!["show", "window", "channel"].some((name) => query.has(name)))
    return null;
  const window = query.get("window");
  return {
    show: listed(query, MAP_LAYERS),
    window: isSeenWindow(window) ? (window as SeenWindow) : DEFAULT_SEEN_WINDOW,
    channel: query.get("channel") || null,
  };
}

function nodeLinkView(query: URLSearchParams): NodeLinkView | null {
  if (!query.has("map") && !query.has("show")) return null;
  return {
    expanded: query.get("map") === "expanded",
    show: listed(query, MINIMAP_LAYERS),
  };
}

/** The map with the node selected and the view's layers, window and channel. */
export function mapViewHref(nodeId: number, view: MapLinkView): string {
  const query = new URLSearchParams();
  if (view.show.length > 0) query.set("show", view.show.join(","));
  query.set("window", view.window);
  if (view.channel !== null) query.set("channel", view.channel);
  return `#map/${formatNodeId(nodeId)}?${decodeCommas(query)}`;
}

/** The node's page with its minimap as the view has it. */
export function nodeViewHref(nodeId: number, view: NodeLinkView): string {
  const query = new URLSearchParams();
  if (view.expanded) query.set("map", "expanded");
  if (view.show.length > 0) query.set("show", view.show.join(","));
  const search = decodeCommas(query);
  return `#nodes/${formatNodeId(nodeId)}${search === "" ? "" : `?${search}`}`;
}

// Commas need no escaping in a URL; left as they are, lists stay readable.
function decodeCommas(query: URLSearchParams): string {
  return query.toString().replaceAll("%2C", ",");
}
