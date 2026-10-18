"""Graph calculations for the Zones page, on networkx graphs of node IDs.

These are pure functions with no database access; zones.py builds the graphs.
Each link carries two attributes: `weight`, which groups nodes into zones, and
`margin`, the link's weaker fade margin in dB, which ranks paths for carriers.

- cuts: how many nodes each node cuts off when it goes quiet, from one
  depth-first search (Tarjan's articulation points).
- zones: groups of well-linked nodes, by Louvain modularity: move nodes into
  the group that most raises "links inside groups versus chance", then treat
  each group as one node and repeat.
- carriers: how many nodes reach a hub through each node on their best path.
- shortest_paths: each node's fewest-hop path from a set of nodes, ties
  broken by margin.

Traceroutes sample the network sparsely, so all of this holds only "as far as
the links show".
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict

import networkx as nx

# Groups smaller than this are folded into the neighbouring zone they link to most.
MIN_ZONE_NODES = 3
# Louvain starts from a random order; a fixed seed gives the same zones for the same links.
ZONE_SEED = 1


def cuts(graph: nx.Graph[int]) -> dict[int, int]:
    """For each node whose loss splits the nodes it is connected to, how many
    of them lose every path to the biggest remainder. Iterative, so long chains
    do not hit Python's recursion limit.
    """
    order: dict[int, int] = {}
    low: dict[int, int] = {}
    size: dict[int, int] = {}
    cut_off: dict[int, int] = {}
    counter = 0
    for root in graph:
        if root in order:
            continue
        # First pass: depth-first order, low links, subtree sizes and children.
        children: dict[int, list[int]] = defaultdict(list)
        order[root] = low[root] = counter
        counter += 1
        stack = [(root, -1, iter(graph[root]))]
        visited = [root]
        while stack:
            node, parent, others = stack[-1]
            advanced = False
            for other in others:
                if other not in order:
                    order[other] = low[other] = counter
                    counter += 1
                    children[node].append(other)
                    visited.append(other)
                    stack.append((other, node, iter(graph[other])))
                    advanced = True
                    break
                if other != parent:
                    low[node] = min(low[node], order[other])
            if advanced:
                continue
            stack.pop()
            size[node] = 1 + sum(size[child] for child in children[node])
            if parent != -1:
                low[parent] = min(low[parent], low[node])
        total = len(visited)
        # Second pass: which parts each node would split off.
        for node in visited:
            parts = [
                size[child] for child in children[node] if node == root or low[child] >= order[node]
            ]
            if node == root and len(children[node]) < 2:
                parts = []
            if parts:
                rest = total - 1 - sum(parts)
                cut_off[node] = total - 1 - max(*parts, rest)
    return cut_off


def zones(graph: nx.Graph[int]) -> dict[int, int]:
    """Zone number of each node, 1 for the biggest zone.

    Louvain groups the nodes by link weight. A group of fewer than
    MIN_ZONE_NODES is folded into the neighbouring group it shares the most
    weight with. Nodes in groups too small to hold a zone get none.
    """
    group_of: dict[int, int] = {}
    groups: dict[int, set[int]] = {}
    for index, members in enumerate(nx.community.louvain_communities(graph, seed=ZONE_SEED)):
        groups[index] = set(members)
        group_of.update(dict.fromkeys(members, index))

    for group in sorted(groups, key=lambda key: (len(groups[key]), min(groups[key]))):
        members = groups[group]
        if not members or len(members) >= MIN_ZONE_NODES:
            continue
        pull: dict[int, float] = defaultdict(float)
        for node in members:
            for other, link in graph[node].items():
                if group_of[other] != group:
                    pull[group_of[other]] += link["weight"]
        if not pull:
            continue  # a connected group too small to be a zone
        target = max(pull, key=lambda key: (pull[key], -key))
        groups[target] |= members
        group_of.update(dict.fromkeys(members, target))
        groups[group] = set()

    kept = [members for members in groups.values() if len(members) >= MIN_ZONE_NODES]
    kept.sort(key=lambda members: (-len(members), min(members)))
    return {node: number for number, members in enumerate(kept, 1) for node in members}


def carriers(graph: nx.Graph[int], roots: list[int]) -> dict[int, int]:
    """For each node, how many nodes reach a root through it on their best path.

    A node's count is the smaller side it splits the root's best-path tree
    into, so the root's own side cannot inflate it, taking the root where that
    is largest. Nodes on no path count 0 and are left out.
    """
    carries: dict[int, int] = defaultdict(int)
    for root in roots:
        order, parent = _best_paths(graph, root)
        size = dict.fromkeys(order, 1)
        for node in reversed(order):
            if (step := parent[node]) is not None:
                size[step] += size[node]
        total = len(order)
        for node in order[1:]:
            carried = min(size[node] - 1, total - size[node])
            if carried > carries.get(node, 0):
                carries[node] = carried
    return dict(carries)


def _best_paths(graph: nx.Graph[int], root: int) -> tuple[list[int], dict[int, int | None]]:
    """Each node's best path to root: the strongest weakest link (by margin),
    then the fewest hops. A Dijkstra-style search, so the hop tie-break is only
    approximate. Returns the nodes in the order they were settled, root first,
    and each node's next step toward root."""
    best: dict[int, tuple[float, int]] = {root: (math.inf, 0)}
    parent: dict[int, int | None] = {root: None}
    queue: list[tuple[float, int, int]] = [(-math.inf, 0, root)]
    order: list[int] = []
    settled: set[int] = set()
    while queue:
        _, hops, node = heapq.heappop(queue)
        if node in settled:
            continue
        settled.add(node)
        order.append(node)
        bottleneck = best[node][0]
        for other, link in graph[node].items():
            if other in settled:
                continue
            candidate = (min(bottleneck, link["margin"]), hops + 1)
            current = best.get(other)
            if current is None or (-candidate[0], candidate[1]) < (-current[0], current[1]):
                best[other] = candidate
                parent[other] = node
                heapq.heappush(queue, (-candidate[0], candidate[1], other))
    return order, parent


def shortest_paths(
    graph: nx.Graph[int], sources: list[int]
) -> tuple[dict[int, int], dict[int, float], dict[int, int | None]]:
    """Each node's fewest-hop path from any of sources, and among those the
    one with the strongest weakest link (by margin). A breadth-first search
    one hop at a time, so both are exact. Returns each reached node's hops,
    its path's weakest margin (infinite for a source) and its previous step
    (None for a source)."""
    hops = {node: 0 for node in sources if node in graph}
    weakest = dict.fromkeys(hops, math.inf)
    previous: dict[int, int | None] = dict.fromkeys(hops)
    frontier = sorted(hops)
    step = 0
    while frontier:
        step += 1
        reached: list[int] = []
        for node in frontier:
            for other, link in graph[node].items():
                if hops.get(other, step) < step:
                    continue
                candidate = min(weakest[node], link["margin"])
                if other not in hops:
                    hops[other] = step
                    reached.append(other)
                elif candidate <= weakest[other]:
                    continue
                weakest[other] = candidate
                previous[other] = node
        frontier = reached
    return hops, weakest, previous
