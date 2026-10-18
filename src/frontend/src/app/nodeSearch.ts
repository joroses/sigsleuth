import { formatNodeId } from "./nodeDisplay";

export const NODE_SEARCH_LIMIT = 10;

interface SearchableNode {
  node_id: number;
  long_name: string | null;
  short_name: string | null;
}

// Case- and accent-insensitive: "José" and "jose" match each other.
function fold(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

// 0 exact, 1 starts with, 2 contains; null when it does not match.
function matchRank(text: string | null, query: string | null): number | null {
  if (text === null || query === null || query === "") return null;
  if (text === query) return 0;
  if (text.startsWith(query)) return 1;
  if (text.includes(query)) return 2;
  return null;
}

/** Each node with the text search compares against, prepared once per node list. */
export type NodeSearchIndex<T> = {
  node: T;
  shortName: string | null;
  longName: string | null;
  hex: string;
  decimal: string;
}[];

export function nodeSearchIndex<T extends SearchableNode>(
  nodes: readonly T[],
): NodeSearchIndex<T> {
  return nodes.map((node) => ({
    node,
    shortName: node.short_name && fold(node.short_name),
    longName: node.long_name && fold(node.long_name),
    hex: formatNodeId(node.node_id).slice(1),
    decimal: String(node.node_id),
  }));
}

/**
 * Nodes whose short or long name, hex node ID (!a1b2c3d4, with or without the !)
 * or decimal node number matches the query, best match first, at most `limit`.
 * Equally good matches keep their input order, which for the map is most
 * recently active first.
 */
export function searchNodes<T>(
  index: NodeSearchIndex<T>,
  query: string,
  limit = NODE_SEARCH_LIMIT,
): T[] {
  const folded = fold(query.trim());
  if (folded === "") return [];
  const hexQuery = /^!?[0-9a-f]+$/.test(folded)
    ? folded.replace(/^!/, "")
    : null;
  const decimalQuery = /^\d+$/.test(folded) ? folded : null;

  const matches: { node: T; rank: number }[] = [];
  for (const entry of index) {
    const ranks = [
      matchRank(entry.shortName, folded),
      matchRank(entry.longName, folded),
      matchRank(entry.hex, hexQuery),
      matchRank(entry.decimal, decimalQuery),
    ].filter((rank) => rank !== null);
    if (ranks.length > 0)
      matches.push({ node: entry.node, rank: Math.min(...ranks) });
  }
  return matches
    .sort((a, b) => a.rank - b.rank)
    .slice(0, limit)
    .map((match) => match.node);
}
