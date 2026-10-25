// What the Links page and a node page's links share: kind names, row order,
// and figures from both directions.

import type { LinkKind, LinkRow, MapLink } from "./api";
import { bothWaysObservations, fadeMargin } from "./linkQuality";

export const LINK_KIND_LABELS: Record<LinkKind, string> = {
  traceroute: "Traceroute hops",
  direct: "Direct gateway reports",
  neighbor: "Neighbour info",
};

export type LinkSort =
  "distance" | "observations" | "weaker_margin" | "asymmetry" | "last_seen";

const LINK_SORTS: {
  sort: LinkSort;
  value: (link: LinkRow) => number | null;
}[] = [
  { sort: "distance", value: (link) => link.distance_km },
  { sort: "observations", value: bothWaysObservations },
  {
    sort: "weaker_margin",
    // Lowest first: negate so the shared "highest first" order works.
    value: (link) => {
      const margin = weakerMargin(link);
      return margin === null ? null : -margin;
    },
  },
  { sort: "asymmetry", value: asymmetry },
  { sort: "last_seen", value: (link) => Date.parse(link.last_seen_at) },
];

export function isLinkSort(value: unknown): boolean {
  return LINK_SORTS.some((item) => item.sort === value);
}

/**
 * Links with no value come last whichever way round. By distance, those whose
 * distance the backend doubts (LinkRow.doubts) come after the measured links
 * when longest first, and at the top when shortest first, to find them; at
 * the top they stay longest first, the most suspicious leading.
 */
export function sortLinks<T extends LinkRow>(
  links: T[],
  sort: LinkSort,
  ascending: boolean,
): T[] {
  const value = LINK_SORTS.find((item) => item.sort === sort)!.value;
  const tier = (link: T) =>
    value(link) === null
      ? 2
      : sort === "distance" && link.doubts.length > 0
        ? ascending
          ? -1
          : 1
        : 0;
  return [...links].sort((a, b) => {
    const byTier = tier(a) - tier(b);
    if (byTier !== 0) return byTier;
    const difference = (value(a) ?? 0) - (value(b) ?? 0);
    return ascending && tier(a) !== -1 ? difference : -difference;
  });
}

/**
 * The weaker direction's average margin; a link heard one way uses that
 * direction. Null when no heard direction has an SNR.
 */
export function weakerMargin(link: MapLink): number | null {
  const margins = [link.a_to_b, link.b_to_a]
    .map((direction) =>
      fadeMargin(direction.average_snr, link.spreading_factor),
    )
    .filter((margin): margin is number => margin !== null);
  return margins.length === 0 ? null : Math.min(...margins);
}

export function asymmetry(link: LinkRow): number | null {
  const a = link.a_to_b.average_snr;
  const b = link.b_to_a.average_snr;
  return a === null || b === null ? null : Math.abs(a - b);
}

export function touches(link: LinkRow, nodeIds: ReadonlySet<number>): boolean {
  return nodeIds.has(link.node_a) || nodeIds.has(link.node_b);
}

/** The link with nodeId as node_a, so the node's figures come first. */
export function fromNode<T extends MapLink>(link: T, nodeId: number): T {
  if (link.node_a === nodeId) return link;
  return {
    ...link,
    node_a: link.node_b,
    node_b: link.node_a,
    a_to_b: link.b_to_a,
    b_to_a: link.a_to_b,
  };
}
