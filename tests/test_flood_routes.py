"""The guessed hops between a packet's relays on hand-built receptions and links."""

from datetime import UTC, datetime

from sigsleuth.db import NodeSummary
from sigsleuth.flood_routes import FloodRouteHop, RouteLinks, route_hops
from sigsleuth.packets import PacketReception, RelayNode

SENDER = 0x0A0A0A01
HOP_START = 7


def reception(
    gateway: int,
    hops: int,
    relay: int | None = None,
    *,
    how: str = "gateway_link",
    direct: bool = False,
) -> PacketReception:
    return PacketReception(
        reception_id=gateway,
        received_at=datetime(2026, 10, 20, tzinfo=UTC),
        gateway_node_id=gateway,
        gateway_name=None,
        gateway_short_name=None,
        gateway_latitude=None,
        gateway_longitude=None,
        gateway_estimated=False,
        rx_snr=None,
        rx_rssi=None,
        hop_start=HOP_START,
        hop_limit=HOP_START - hops,
        relay_byte=None if relay is None else relay & 0xFF,
        paths=None,
        direct=direct,
        relay_node=None if relay is None else RelayNode(relay, None, 1, how),  # type: ignore[arg-type]
    )


def node(node_id: int, latitude: float | None = None) -> NodeSummary:
    return NodeSummary(
        node_id, None, None, "ROUTER", latitude, None if latitude is None else 19.0, False
    )


def walk(
    receptions: list[PacketReception],
    partners: dict[int, set[int]] | None = None,
    heard: dict[int, dict[int, int]] | None = None,
    *,
    hidden: frozenset[int] = frozenset(),
    places: dict[int, NodeSummary] | None = None,
) -> list[tuple[int, int, int, str]]:
    asked: list[set[int]] = []

    def links(nodes: set[int]) -> RouteLinks:
        asked.append(nodes)
        return RouteLinks(
            {n: (partners or {}).get(n, set()) for n in nodes},
            {n: (heard or {}).get(n, {}) for n in nodes},
        )

    found = route_hops(SENDER, receptions, places or {}, links, hidden)
    return [(hop.from_node_id, hop.to_node_id, hop.step, hop.how) for hop in found]


def test_a_relay_after_one_hop_heard_the_sender() -> None:
    assert walk([reception(0xAA, 1, 0xB1)]) == [(SENDER, 0xB1, 0, "one_hop")]


def test_a_relay_that_is_a_gateway_names_whom_it_heard_over_the_score() -> None:
    receptions = [
        reception(0xAA, 2, 0xB2),
        # B2's own report: it heard B1 directly after one hop.
        reception(0xB2, 1, 0xB1, how="heard_directly"),
    ]
    # The score alone would pick C: B2 heard it in many traceroutes.
    found = walk(receptions, {0xB2: {0xC0}}, {0xB2: {0xC0: 50}})
    assert found == [(SENDER, 0xB1, 0, "one_hop"), (0xB1, 0xB2, 1, "heard_directly")]


def test_a_gateways_report_through_a_guessed_relay_is_a_guess() -> None:
    receptions = [reception(0xAA, 2, 0xB2), reception(0xB2, 1, 0xB1)]
    assert (0xB1, 0xB2, 1, "reported") in walk(receptions)


def test_a_sure_score_names_the_relay_one_hop_earlier() -> None:
    receptions = [reception(0xAA, 2, 0xB2), reception(0xAB, 1, 0xB1)]
    found = route_hops(
        SENDER,
        receptions,
        {},
        lambda nodes: RouteLinks({0xB2: {0xB1, 0xC0}}, {0xB2: {0xB1: 20}}),
        set(),
    )
    probable = next(hop for hop in found if hop.to_node_id == 0xB2)
    assert (probable.from_node_id, probable.step, probable.how) == (0xB1, 1, "probable")
    assert probable.probability is not None and probable.probability >= 0.99
    assert FloodRouteHop(SENDER, 0xB1, 0, "one_hop", None) in found


def test_an_unsure_score_leaves_a_gap() -> None:
    receptions = [reception(0xAA, 3, 0xB3)]
    found = walk(receptions, {0xB3: {0xC1, 0xC2}}, {0xB3: {0xC1: 10, 0xC2: 10}})
    assert found == []


def test_a_hidden_best_candidate_stops_the_walk_rather_than_name_the_next() -> None:
    receptions = [reception(0xAA, 3, 0xB3)]
    partners = {0xB3: {0xC1, 0xC2}}
    heard = {0xB3: {0xC1: 40, 0xC2: 3}}
    # C1 is far ahead of C2, and C2 well ahead of every other candidate.
    assert walk(receptions, partners, heard)[0][:2] == (0xC1, 0xB3)
    assert walk(receptions, partners, heard, hidden=frozenset({0xC1})) == []


def test_a_walk_never_comes_back_to_a_node_it_passed() -> None:
    receptions = [reception(0xAA, 3, 0xB3)]
    # C heard B3 as often as D, but B3 is where the walk came from.
    partners = {0xB3: {0xC0}, 0xC0: {0xB3, 0xD0}}
    heard = {0xB3: {0xC0: 30}, 0xC0: {0xB3: 30, 0xD0: 30}}
    assert walk(receptions, partners, heard) == [
        (SENDER, 0xD0, 0, "one_hop"),
        (0xD0, 0xC0, 1, "probable"),
        (0xC0, 0xB3, 2, "probable"),
    ]


def test_a_candidate_at_an_impossible_level_loses() -> None:
    # B1 was named after one hop, so it cannot have sent B4 its copy after three.
    receptions = [reception(0xAA, 4, 0xB4), reception(0xAB, 1, 0xB1)]
    partners = {0xB4: {0xB1, 0xC0}}
    heard = {0xB4: {0xB1: 5, 0xC0: 5}}
    found = walk(receptions, partners, heard)
    assert (0xC0, 0xB4, 3, "probable") in found


def test_a_candidate_back_toward_the_sender_wins() -> None:
    receptions = [reception(0xAA, 3, 0xB3)]
    places = {SENDER: node(SENDER, 50.0), 0xB3: node(0xB3, 50.5)}
    places |= {0xC1: node(0xC1, 50.1), 0xC2: node(0xC2, 51.5)}
    partners = {0xB3: {0xC1, 0xC2}}
    heard = {0xB3: {0xC1: 10, 0xC2: 10}}
    assert walk(receptions, partners, heard, places=places)[0][:2] == (0xC1, 0xB3)
