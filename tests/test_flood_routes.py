"""The guessed hops between a packet's relays on hand-built receptions and links."""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from derived_rows import insert_row, link_hour
from meshtastic_fixtures import application_envelope
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import NodeSummary, migrate_database, open_current_database
from sigsleuth.flood_routes import (
    FloodRouteHop,
    OneHopLinks,
    RouteLinks,
    one_hop_links,
    route_hops,
)
from sigsleuth.ingest import MqttDelivery, ingest_delivery
from sigsleuth.links import LINK_WINDOW_MS
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


def node(node_id: int, latitude: float | None = None, role: str = "ROUTER") -> NodeSummary:
    return NodeSummary(
        node_id, None, None, role, latitude, None if latitude is None else 19.0, False
    )


def no_one_hop_links(nodes: set[int]) -> OneHopLinks:
    return OneHopLinks()


def walk(
    receptions: list[PacketReception],
    partners: dict[int, set[int]] | None = None,
    heard: dict[int, dict[int, int]] | None = None,
    *,
    hidden: frozenset[int] = frozenset(),
    places: dict[int, NodeSummary] | None = None,
    heard_sender: frozenset[int] = frozenset(),
    free_from: dict[int, set[int]] | None = None,
) -> list[tuple[int, int, int, str]]:
    """The hops found; heard_sender and free_from are what OneHopLinks
    reads for the nodes asked about."""

    def links(nodes: set[int]) -> RouteLinks:
        return RouteLinks(
            {n: (partners or {}).get(n, set()) for n in nodes},
            {n: (heard or {}).get(n, {}) for n in nodes},
        )

    def one_hop(nodes: set[int]) -> OneHopLinks:
        return OneHopLinks(
            set(heard_sender) & nodes,
            {n: (free_from or {})[n] for n in nodes if n in (free_from or {})},
        )

    found = route_hops(SENDER, receptions, places or {}, links, one_hop, hidden)
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
        no_one_hop_links,
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


def test_a_relay_after_a_0_cost_hop_goes_on_at_its_own_level() -> None:
    # S -> B1 -> B2 -> B3, B3 relaying for free: B2's and B3's copies both
    # used two hops.
    receptions = [reception(0xA1, 1, 0xB1), reception(0xA2, 2, 0xB2), reception(0xA3, 2, 0xB3)]
    heard = {0xB2: {0xB1: 50}, 0xB3: {0xB2: 50}}
    assert walk(receptions, {}, heard) == [
        (SENDER, 0xB1, 0, "one_hop"),
        (0xB1, 0xB2, 1, "probable"),
        (0xB2, 0xB3, 2, "probable"),
    ]


def test_an_own_report_heard_at_the_relays_level_shows_at_that_level() -> None:
    # B1 heard C1's copy after one hop, so C1 was no sender.
    receptions = [reception(0xAA, 1, 0xB1), reception(0xB1, 1, 0xC1)]
    assert walk(receptions) == [(SENDER, 0xC1, 0, "one_hop"), (0xC1, 0xB1, 1, "reported")]

    receptions = [
        reception(0xAA, 2, 0xB2),
        reception(0xB2, 2, 0xC2, how="heard_directly"),
        reception(0xAB, 1, 0xB1),
    ]
    assert walk(receptions, {}, {0xC2: {0xB1: 50}}) == [
        (SENDER, 0xB1, 0, "one_hop"),
        (0xB1, 0xC2, 1, "probable"),
        (0xC2, 0xB2, 2, "heard_directly"),
    ]


def test_a_relay_heard_at_both_levels_goes_on_a_level_lower() -> None:
    receptions = [reception(0xAA, 2, 0xB3), reception(0xAB, 1, 0xB2), reception(0xAC, 2, 0xB2)]
    assert walk(receptions, {}, {0xB3: {0xB2: 50}}) == [
        (SENDER, 0xB2, 0, "one_hop"),
        (0xB2, 0xB3, 1, "probable"),
    ]


# R2 is a router named after one hop, as is R1, whose copies R2 passes on for free.
R1, R2 = 0xB1, 0xB2
ROUTERS = {R1: node(R1), R2: node(R2)}
AFTER_ONE_HOP = [reception(0xAA, 1, R1), reception(0xAB, 1, R2)]


def test_a_router_that_never_heard_the_sender_passed_on_its_one_free_partner() -> None:
    found = walk(AFTER_ONE_HOP, places=ROUTERS, free_from={R2: {R1}})
    # Then on from R1 at its own level, to the sender.
    assert found == [(SENDER, R1, 0, "one_hop"), (R1, R2, 1, "zero_cost")]


def test_a_free_partner_that_heard_the_sender_directly_still_shows_at_step_1() -> None:
    receptions = [reception(R1, 0, direct=True), reception(0xAB, 1, R2)]
    found = walk(receptions, places=ROUTERS, free_from={R2: {R1}})
    assert found == [(SENDER, R1, 0, "heard_directly"), (R1, R2, 1, "zero_cost")]


def test_a_router_heard_hearing_the_sender_heard_the_sender() -> None:
    found = walk(AFTER_ONE_HOP, places=ROUTERS, heard_sender=frozenset({R2}), free_from={R2: {R1}})
    assert (SENDER, R2, 0, "one_hop") in found


def test_only_router_roles_pass_on_for_free() -> None:
    places = {**ROUTERS, R2: node(R2, role="CLIENT")}
    found = walk(AFTER_ONE_HOP, places=places, free_from={R2: {R1}})
    assert (SENDER, R2, 0, "one_hop") in found


def test_a_router_without_a_free_partner_keeps_the_sender() -> None:
    assert (SENDER, R2, 0, "one_hop") in walk(AFTER_ONE_HOP, places=ROUTERS)


@pytest.mark.parametrize("partner_hops", [None, 2])
def test_a_free_partner_missing_at_one_hop_leaves_a_gap(partner_hops: int | None) -> None:
    receptions = [reception(0xAB, 1, R2)]
    if partner_hops is not None:
        receptions.append(reception(0xAA, partner_hops, R1))
    found = walk(receptions, places=ROUTERS, free_from={R2: {R1}})
    assert not any(hop[1] == R2 for hop in found)


def test_a_routers_own_report_wins_over_a_missing_free_partner() -> None:
    receptions = [reception(0xAB, 1, R2), reception(R2, 0, direct=True)]
    assert walk(receptions, places=ROUTERS, free_from={R2: {R1}}) == [
        (SENDER, R2, 0, "heard_directly")
    ]


def test_two_free_partners_leave_a_gap() -> None:
    receptions = [*AFTER_ONE_HOP, reception(0xAC, 1, 0xB3)]
    found = walk(receptions, places=ROUTERS, free_from={R2: {R1, 0xB3}})
    assert [hop for hop in found if hop[1] == R2] == []


def test_a_hidden_free_partner_leaves_a_gap() -> None:
    found = walk(AFTER_ONE_HOP, places=ROUTERS, free_from={R2: {R1}}, hidden=frozenset({R1}))
    assert found == []


def test_mutual_free_partners_leave_gaps() -> None:
    assert walk(AFTER_ONE_HOP, places=ROUTERS, free_from={R1: {R2}, R2: {R1}}) == []


def test_a_gateways_report_resolves_mutual_free_partners() -> None:
    receptions = [*AFTER_ONE_HOP, reception(R1, 0, direct=True)]
    assert walk(receptions, places=ROUTERS, free_from={R1: {R2}, R2: {R1}}) == [
        (SENDER, R1, 0, "heard_directly"),
        (R1, R2, 1, "zero_cost"),
    ]


def test_a_reported_direction_wins_over_a_reverse_free_guess() -> None:
    receptions = [*AFTER_ONE_HOP, reception(R1, 1, R2, how="heard_directly")]
    assert walk(receptions, places=ROUTERS, free_from={R2: {R1}}) == [(R2, R1, 1, "heard_directly")]


def test_a_report_naming_a_hidden_predecessor_leaves_a_gap() -> None:
    receptions = [reception(0xAA, 2, R2), reception(R2, 1, R1)]
    assert walk(receptions, places=ROUTERS, hidden=frozenset({R1})) == []


@pytest.mark.parametrize("partner_count", [1, 2])
def test_route_does_not_replace_a_hidden_free_partner(tmp_path: Path, partner_count: int) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    at = 1_790_000_000_000
    r3 = 0xB3
    with closing(open_current_database(path)) as connection:
        for relay, gateway in ((R1, 0xAA), (R2, 0xAB), (r3, 0xAC)):
            gateway_id = f"!{gateway:08x}"
            topic = f"msh/EU_868/2/e/LongFast/{gateway_id}"
            direct = application_envelope(
                portnums_pb2.PortNum.NODEINFO_APP,
                mesh_pb2.User(role=2).SerializeToString(),
                sender=relay,
                gateway_id=gateway_id,
                bitfield=1,
                packet_id=10,
                hop_start=3,
                hop_limit=3,
                relay_node=relay,
                rx_snr=6.0,
            )
            ingest_delivery(connection, MqttDelivery(at - 1000, topic, direct))
            relayed = application_envelope(
                portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                b"hello",
                sender=SENDER,
                gateway_id=gateway_id,
                bitfield=1,
                packet_id=99,
                hop_start=3,
                hop_limit=2,
                relay_node=relay,
                rx_snr=6.0,
            )
            packet = ingest_delivery(connection, MqttDelivery(at, topic, relayed))
        for partner in (R1, r3)[:partner_count]:
            insert_row(
                connection,
                "hop_cost_hour",
                hour_start=at - at % 3_600_000,
                from_node_id=partner,
                to_node_id=R2,
                channel="LongFast",
                free=3,
                costly=0,
            )

    client = TestClient(create_app(Settings(path)))
    url = f"/api/v1/packets/{packet.packet_row_id}"
    before = client.get(f"{url}/flood-routes").json()
    into_r2 = [hop for hop in before["hops"] if hop["to_node_id"] == R2]
    assert [hop["from_node_id"] for hop in into_r2] == ([R1] if partner_count == 1 else [])

    with closing(open_current_database(path)) as connection:
        insert_row(connection, "node_opt_out", node_id=R1)

    after = client.get(f"{url}/flood-routes").json()
    assert not any(hop["to_node_id"] == R2 for hop in after["hops"])
    assert all(R1 not in (hop["from_node_id"], hop["to_node_id"]) for hop in after["hops"])
    assert all(node["node_id"] != R1 for node in after["nodes"])
    detail = client.get(url).json()
    assert detail["receptions"][0]["relay_node"] is None
    assert all(node["node_id"] != R1 for node in detail["relay_nodes"])


def test_one_hop_links_read_free_hops_on_the_packets_channel_and_links_on_any(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    at = 1_790_000_000_000
    hour = at - at % 3_600_000

    def hop_costs(a: int, b: int, free: int, costly: int, channel: str = "LongFast") -> None:
        insert_row(
            connection,
            "hop_cost_hour",
            hour_start=hour,
            from_node_id=a,
            to_node_id=b,
            channel=channel,
            free=free,
            costly=costly,
        )

    with closing(open_current_database(path)) as connection:
        hop_costs(R1, R2, 2, 2)
        hop_costs(0xB3, R2, 1, 2)
        hop_costs(0xB4, R2, 1, 0, "MediumFast")
        hop_costs(0xB5, 0xB6, 1, 0)
        link_hour(connection, SENDER, 0xB6, at, channel="MediumFast")
        link_hour(connection, SENDER, R2, at + LINK_WINDOW_MS + 3_600_000)
        found = one_hop_links(connection, SENDER, {R2, 0xB6}, at, "LongFast")

    assert found == OneHopLinks({0xB6}, {R2: {R1}, 0xB6: {0xB5}})
