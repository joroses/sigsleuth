from __future__ import annotations

import itertools

import networkx as nx

from sigsleuth.graph import carriers, cuts, shortest_paths, zones


def graph(*links: tuple[int, int]) -> nx.Graph[int]:
    built: nx.Graph[int] = nx.Graph()
    built.add_edges_from(links, weight=1.0, margin=10.0)
    return built


def clique(first: int, size: int) -> list[tuple[int, int]]:
    return list(itertools.combinations(range(first, first + size), 2))


def test_the_middle_of_a_chain_of_five_cuts_off_two() -> None:
    assert cuts(graph((1, 2), (2, 3), (3, 4), (4, 5))) == {2: 1, 3: 2, 4: 1}


def test_two_cliques_joined_by_a_path_become_two_zones() -> None:
    zone_of = zones(graph(*clique(1, 5), *clique(11, 5), (5, 20), (20, 11)))

    assert set(zone_of.values()) == {1, 2}
    assert len({zone_of[node] for node in range(1, 6)}) == 1
    assert len({zone_of[node] for node in range(11, 16)}) == 1
    assert zone_of[20] in {1, 2}  # a lone relay is folded into a zone


def test_a_connected_pair_of_two_nodes_is_not_a_zone() -> None:
    zone_of = zones(graph(*clique(1, 5), (30, 31)))

    assert 30 not in zone_of and 31 not in zone_of


def test_a_relay_with_four_leaves_behind_it_carries_four() -> None:
    hub_side = [(1, 2), (1, 3), (1, 4), (2, 3)]
    carries = carriers(graph(*hub_side, (1, 10), *((10, leaf) for leaf in range(11, 15))), [1])

    assert carries[10] == 4


def test_nodes_that_carry_no_one_are_left_out() -> None:
    assert carriers(graph(*clique(1, 3)), [1]) == {}


def test_a_shortest_path_takes_the_fewest_hops_then_the_strongest_weakest_link() -> None:
    # From 1 to 5: 1-2-5 and 1-3-5 both take two hops, and 1-3-5's weakest
    # link is stronger; 1-4-6-5 is stronger still but takes three.
    paths: nx.Graph[int] = nx.Graph()
    for a, b, margin in [
        (1, 2, 10),
        (2, 5, 3),
        (1, 3, 8),
        (3, 5, 9),
        (1, 4, 20),
        (4, 6, 20),
        (6, 5, 20),
    ]:
        paths.add_edge(a, b, margin=margin)

    hops, weakest, previous = shortest_paths(paths, [1])

    assert (hops[5], weakest[5], previous[5]) == (2, 8, 3)
