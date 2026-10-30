import type { MapLink, MapNode } from "./api";
import { nodeCategory, type NodeCategory } from "./mapNodeLayers";
import { formatRole } from "./nodeDisplay";

// Select values for "every node" and "the node did not report one"; real roles
// are upper-case enum names and channel names are never a lone symbol.
export const ANY = "*";
export const NOT_REPORTED = "-";

export function onChosenChannel(
  link: Pick<MapLink, "channel">,
  channel: string,
): boolean {
  return (
    channel === ANY ||
    link.channel === (channel === NOT_REPORTED ? "Unknown" : channel)
  );
}

/**
 * Filters on a node's marker category, reported role and radio channel, and
 * whether nodes drawn at an estimated position show; a node
 * must pass them all.
 */
export interface NodeFilter {
  categories: NodeCategories;
  role: string;
  channel: string;
  estimated: boolean;
}

/** Which marker categories are shown. */
export type NodeCategories = Record<NodeCategory, boolean>;

const ALL_CATEGORIES: NodeCategories = {
  relay: true,
  gateway: true,
  other: true,
};

/** Shows every node. */
export const DEFAULT_FILTER: NodeFilter = {
  categories: ALL_CATEGORIES,
  role: ANY,
  channel: ANY,
  estimated: true,
};

/** The channel most of the nodes last used, or null when none reported one. */
export function busiestChannel(nodes: MapNode[]): string | null {
  let busiest: string | null = null;
  let most = 0;
  for (const [channel, count] of tally(nodes, channelOf))
    if (channel !== NOT_REPORTED && count > most) {
      busiest = channel;
      most = count;
    }
  return busiest;
}

export interface FilterOption {
  value: string;
  label: string;
  count: number;
}

function roleOf(node: MapNode): string {
  return node.role ?? NOT_REPORTED;
}

function channelOf(node: MapNode): string {
  return node.channel ?? NOT_REPORTED;
}

function tally(
  nodes: MapNode[],
  valueOf: (node: MapNode) => string,
): Map<string, number> {
  const counts = new Map<string, number>();
  for (const node of nodes) {
    const value = valueOf(node);
    counts.set(value, (counts.get(value) ?? 0) + 1);
  }
  return counts;
}

/**
 * Every value the loaded nodes have, most common first, with how many of the
 * counted nodes have each. The order ignores the counted subset so the list
 * does not reshuffle as other filters change.
 */
function options(
  nodes: MapNode[],
  counted: MapNode[],
  valueOf: (node: MapNode) => string,
  label: (value: string) => string,
): FilterOption[] {
  const totals = tally(nodes, valueOf);
  const counts = tally(counted, valueOf);
  return [...totals]
    .sort(
      ([a, aTotal], [b, bTotal]) =>
        bTotal - aTotal || label(a).localeCompare(label(b)),
    )
    .map(([value]) => ({
      value,
      label: label(value),
      count: counts.get(value) ?? 0,
    }));
}

function roleLabel(value: string): string {
  return formatRole(value === NOT_REPORTED ? null : value);
}

function channelLabel(value: string): string {
  return value === NOT_REPORTED ? "Unknown" : value;
}

/** How many of the given nodes fall in each marker category. */
export function categoryCounts(nodes: MapNode[]): Record<NodeCategory, number> {
  const counts = { relay: 0, gateway: 0, other: 0 };
  for (const node of nodes) counts[nodeCategory(node)] += 1;
  return counts;
}

/**
 * The numbers beside each filter choice: how many loaded nodes the choice
 * would show with the other filters as they are. Each filter ignores its own
 * setting, so a switched-off category or unchosen role still says how many
 * nodes it would add.
 */
export function filterCounts(
  nodes: MapNode[],
  filter: NodeFilter,
): {
  categories: Record<NodeCategory, number>;
  roles: FilterOption[];
  channels: FilterOption[];
  estimated: number;
} {
  const matching = (without: Partial<NodeFilter>) =>
    nodes.filter((node) => matchesNodeFilter(node, { ...filter, ...without }));
  return {
    categories: categoryCounts(matching({ categories: ALL_CATEGORIES })),
    roles: options(nodes, matching({ role: ANY }), roleOf, roleLabel),
    channels: options(
      nodes,
      matching({ channel: ANY }),
      channelOf,
      channelLabel,
    ),
    estimated: matching({ estimated: true }).filter(
      (node) => node.estimate !== null,
    ).length,
  };
}

export function matchesNodeFilter(node: MapNode, filter: NodeFilter): boolean {
  return (
    filter.categories[nodeCategory(node)] &&
    (filter.estimated || node.estimate === null) &&
    (filter.role === ANY || roleOf(node) === filter.role) &&
    (filter.channel === ANY || channelOf(node) === filter.channel)
  );
}

/**
 * The filter a node must pass to end a link shown. A chosen channel is left
 * out, as links are filtered by their own channel: a node's radio channel is
 * only its latest one, so a node that switched preset, or one seen on no
 * radio channel, still carries links on the mesh's channel. "Not reported" stays, as no
 * link lacks a channel: it keeps the links between nodes with no channel.
 */
export function linkEndFilter(filter: NodeFilter): NodeFilter {
  return filter.channel === NOT_REPORTED ? filter : { ...filter, channel: ANY };
}

/**
 * The loaded nodes links may end at: those linkEndFilter keeps, and the node
 * #map/<node number> asked for (keepId).
 */
export function linkableNodes(
  nodes: MapNode[],
  filter: NodeFilter,
  keepId: number | null,
): MapNode[] {
  const endFilter = linkEndFilter(filter);
  return nodes.filter(
    (node) => node.node_id === keepId || matchesNodeFilter(node, endFilter),
  );
}

/**
 * The nodes the map draws: those every filter keeps, the node keepId, and the
 * linkable nodes at either end of a link shown (linkEnds), so no line ends
 * where no node is drawn.
 */
export function shownNodes(
  nodes: MapNode[],
  filter: NodeFilter,
  keepId: number | null,
  linkEnds: ReadonlySet<number>,
): MapNode[] {
  const endFilter = linkEndFilter(filter);
  return nodes.filter(
    (node) =>
      node.node_id === keepId ||
      matchesNodeFilter(node, filter) ||
      (linkEnds.has(node.node_id) && matchesNodeFilter(node, endFilter)),
  );
}
