import type { LinkKind, MapLink, ZeroCostHop } from "./api";
import { linkKey, zeroCostKey, zeroCostSet } from "./mapLinkLayers";

export const MAX_HOP_DEPTH = 7;

/**
 * The nodes and links within `depth` relays of a node, walking the given links
 * both ways, whatever their direction or link kind. Depth 0 keeps the node's own
 * links and the nodes at their other end; each step adds one relay, so nodes up
 * to depth + 1 links away are kept. Chained links need not be one real path:
 * they may come from different hours and directions.
 */
export function withinHops(
  nodeId: number,
  links: { linkKind: LinkKind; link: MapLink }[],
  depth: number,
): { nodeIds: number[]; linkKeys: string[] } {
  const neighbours = new Map<number, number[]>();
  const connect = (from: number, to: number) => {
    const list = neighbours.get(from);
    if (list === undefined) neighbours.set(from, [to]);
    else list.push(to);
  };
  for (const { link } of links) {
    connect(link.node_a, link.node_b);
    connect(link.node_b, link.node_a);
  }
  // Breadth-first, so each node's distance is its fewest links from nodeId.
  const distance = new Map([[nodeId, 0]]);
  let frontier = [nodeId];
  for (let step = 1; step <= depth + 1 && frontier.length > 0; step++) {
    const next: number[] = [];
    for (const node of frontier)
      for (const neighbour of neighbours.get(node) ?? [])
        if (!distance.has(neighbour)) {
          distance.set(neighbour, step);
          next.push(neighbour);
        }
    frontier = next;
  }
  // A link from a node within depth relays reaches one step further, so its
  // other end was reached too; links between the outermost nodes are left out.
  const inside = (node: number) => (distance.get(node) ?? Infinity) <= depth;
  return {
    nodeIds: [...distance.keys()],
    linkKeys: links
      .filter(({ link }) => inside(link.node_a) || inside(link.node_b))
      .map(({ linkKind, link }) => linkKey(linkKind, link)),
  };
}

// Meshtastic's highest hop limit.
const MAX_ROUTE_HOPS = 7;

// −log of the chance of taking a link one way, so the cheapest route is the
// likeliest. The 0.2 keeps a way never heard usable: the link was heard the
// other way. Revisit: goal is to predict the route a traceroute takes; on live
// loranet.pl (links 2026-10-25..28 vs traceroutes 10-28..31), 10 instead of 3
// or adding the SNR margin did no better.
function stepCost(observations: number): number {
  return -Math.log((observations + 0.2) / (observations + 3.2));
}

/**
 * The likeliest route (stepCost) a packet sent with the highest hop limit can
 * take, with its hops as Meshtastic counts them: each relay spends one, except
 * a relay over a 0-hop direction after the first. A relay needs a hop left,
 * even a 0-hop one. Null when none connects them.
 */
export function findRoute<T extends { link: MapLink }>(
  from: number,
  to: number,
  links: readonly T[],
  zeroCost: readonly ZeroCostHop[] = [],
): { links: T[]; hops: number } | null {
  if (from === to) return null;
  const free = zeroCostSet(zeroCost);
  const steps = new Map<
    number,
    { to: number; item: T; cost: number; free: boolean }[]
  >();
  const addStep = (a: number, b: number, item: T, observations: number) => {
    const step = {
      to: b,
      item,
      cost: stepCost(observations),
      free: free.has(zeroCostKey(a, b, item.link.channel)),
    };
    const list = steps.get(a);
    if (list === undefined) steps.set(a, [step]);
    else list.push(step);
  };
  for (const item of links) {
    const { node_a, node_b, a_to_b, b_to_a } = item.link;
    addStep(node_a, node_b, item, a_to_b.observations);
    addStep(node_b, node_a, item, b_to_a.observations);
  }
  // A way to a node: the hops spent before it, and whether its own relay is
  // 0-hop. The cheapest way to each such state, improved until none changes.
  type Way = {
    node: number;
    spent: number;
    free: boolean;
    cost: number;
    item: T | null;
    back: Way | null;
  };
  const state = (way: Way) => `${way.node}:${way.spent}:${way.free}`;
  const best = new Map<string, Way>();
  const start: Way = {
    node: from,
    spent: 0,
    free: false,
    cost: 0,
    item: null,
    back: null,
  };
  let found: Way | null = null;
  let changed = [start];
  while (changed.length > 0) {
    const next: Way[] = [];
    for (const way of changed) {
      if (way !== start && best.get(state(way)) !== way) continue;
      if (way.spent >= MAX_ROUTE_HOPS) continue;
      const spent = way === start ? 0 : way.spent + (way.free ? 0 : 1);
      for (const step of steps.get(way.node) ?? []) {
        const reached: Way = {
          node: step.to,
          spent,
          free: way !== start && step.free,
          cost: way.cost + step.cost,
          item: step.item,
          back: way,
        };
        if (step.to === from || (found !== null && reached.cost >= found.cost))
          continue;
        if (step.to === to) {
          found = reached;
          continue;
        }
        const known = best.get(state(reached));
        if (known !== undefined && known.cost <= reached.cost) continue;
        best.set(state(reached), reached);
        next.push(reached);
      }
    }
    changed = next;
  }
  if (found === null) return null;
  // Every link costs something, so the cheapest way never visits a node twice.
  const route: T[] = [];
  for (let way: Way | null = found; way !== null; way = way.back)
    if (way.item !== null) route.unshift(way.item);
  return { links: route, hops: found.spent };
}
