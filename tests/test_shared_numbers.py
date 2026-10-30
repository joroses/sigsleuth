import base64
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from derived_rows import insert_row, link_hour
from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database, recreate_derived, to_epoch_ms
from sigsleuth.derive import rederive
from sigsleuth.links import link_table, map_links
from sigsleuth.packets import nodes_when_heard
from sigsleuth.positions import direct_partners, own_reach, positions_at, refresh_estimates
from sigsleuth.retention import apply_retention
from sigsleuth.shared_numbers import refresh_shared_numbers

START = datetime(2026, 10, 1, tzinfo=UTC)
NODE = 0x2468ACE0
MAC_A, MAC_B = bytes.fromhex("0a1b2c3d4e5f"), bytes.fromhex("f5e4d3c2b1a0")
GW_A, GW_B, GW_C = 0xAAAAAAAA, 0xBBBBBBBB, 0xCCCCCCCC
NODE_INFO = portnums_pb2.PortNum.NODEINFO_APP
POSITION = portnums_pb2.PortNum.POSITION_APP
TEXT = portnums_pb2.PortNum.TEXT_MESSAGE_APP


@pytest.fixture
def path(tmp_path: Path) -> Path:
    result = tmp_path / "history.db"
    migrate_database(result)
    return result


@pytest.fixture
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_current_database(path)) as connection:
        yield connection


def send(
    path: Path,
    minute: int,
    portnum: int,
    payload: bytes,
    *,
    counter: int,
    gateway: int,
    channel: str = "MediumFast",
    node: int = NODE,
) -> None:
    """A packet the gateway heard directly, its ID's low 10 bits the counter."""
    gateway_id = f"!{gateway:08x}"
    ingest(
        path,
        application_envelope(
            portnum,
            payload,
            sender=node,
            packet_id=((minute + 1000) << 10) | counter,
            channel_id=channel,
            gateway_id=gateway_id,
            hop_start=3,
            hop_limit=3,
            relay_node=node & 0xFF,
            rx_snr=5.0,
        ),
        received_at=START + timedelta(minutes=minute),
        topic=f"msh/EU_868/2/e/{channel}/{gateway_id}",
    )


def node_info(path: Path, minute: int, mac: bytes, name: str, **where: Any) -> None:
    user = mesh_pb2.User(long_name=name, macaddr=mac, hw_model=mesh_pb2.HardwareModel.TBEAM)
    send(path, minute, NODE_INFO, user.SerializeToString(), **where)


def position(path: Path, minute: int, latitude: float, **where: Any) -> None:
    payload = mesh_pb2.Position(latitude_i=int(latitude * 1e7), longitude_i=int(21e7))
    send(path, minute, POSITION, payload.SerializeToString(), **where)


A: dict[str, Any] = {"gateway": GW_A, "channel": "MediumFast"}
B: dict[str, Any] = {"gateway": GW_B, "channel": "NarrowFast"}


def two_radios_at_once(path: Path) -> None:
    """Hilltop (MAC_A, gateway A, MediumFast) and Valley (MAC_B, gateway
    B, NarrowFast) on one number; Valley's NodeInfo comes inside Hilltop's
    counter run 101 -> 103, with a phone-numbered text in between."""
    node_info(path, 0, MAC_A, "Hilltop", counter=100, **A)
    position(path, 10, 52.1, counter=101, **A)
    send(path, 15, TEXT, b"hi", counter=900, **A)
    node_info(path, 20, MAC_B, "Valley", counter=700, **B)
    position(path, 25, 50.0, counter=701, **B)
    position(path, 40, 52.2, counter=103, **A)
    # Only the channel names a radio: gateway C heard no NodeInfo.
    send(path, 50, TEXT, b"hi", counter=950, gateway=GW_C, channel="MediumFast")
    # Gateway A names Hilltop, the channel Valley: left unsorted.
    send(path, 55, TEXT, b"hi", counter=960, gateway=GW_A, channel="NarrowFast")


def refresh(path: Path) -> int:
    with closing(open_current_database(path)) as connection:
        return refresh_shared_numbers(connection)


def get(path: Path, url: str) -> Any:
    with TestClient(create_app(Settings(path))) as client:
        response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


def minute_of(packet: dict[str, Any]) -> int:
    at = datetime.fromisoformat(packet["first_received_at"])
    return int((at - START).total_seconds() // 60)


def test_a_nodeinfo_inside_another_radios_run_shares_the_number(path: Path) -> None:
    two_radios_at_once(path)
    assert refresh(path) == 1

    radios = get(path, f"/api/v1/nodes/{NODE}")["radios"]
    assert radios["shared"]
    assert radios["proof_at"] == (START + timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    assert [
        (r["number"], r["long_name"], r["sorted_packets"], r["position"]["latitude"])
        for r in radios["items"]
    ] == [(1, "Hilltop", 5, 52.2), (2, "Valley", 2, 50.0)]
    assert radios["unsorted_packets"] == 1

    packets = get(path, f"/api/v1/nodes/{NODE}/packets")["packets"]
    sorted_to = {
        minute_of(p): (p["sender_radio"]["number"], p["sender_radio"]["reason"]) for p in packets
    }
    assert sorted_to == {
        0: (1, "node_info"),
        10: (1, "gateways"),
        15: (1, "gateways"),
        20: (2, "node_info"),
        25: (2, "gateways"),
        40: (1, "gateways"),
        50: (1, "channel"),
        55: (None, None),
    }
    detail = get(path, f"/api/v1/packets/{packets[0]['packet_row_id']}")
    assert detail["packet"]["sender_radio"] == {"number": None, "name": None, "reason": None}


def test_radio_filter_pages_sorted_and_unsorted_packets(path: Path) -> None:
    two_radios_at_once(path)
    refresh(path)
    url = f"/api/v1/nodes/{NODE}/packets"

    assert [minute_of(p) for p in get(path, f"{url}?radio=2")["packets"]] == [25, 20]
    assert [minute_of(p) for p in get(path, f"{url}?radio=unsorted")["packets"]] == [55]
    first = get(path, f"{url}?radio=1&limit=2")
    assert [minute_of(p) for p in first["packets"]] == [50, 40]
    rest = get(path, f"{url}?radio=1&before_id={first['next_before_id']}")
    assert [minute_of(p) for p in rest["packets"]] == [15, 10, 0]
    assert get(path, f"{url}?radio=1&port={POSITION}")["packets"][0]["portnum"] == "POSITION_APP"
    assert get(path, f"{url}?radio=3")["packets"] == []
    with TestClient(create_app(Settings(path))) as client:
        assert client.get(f"{url}?radio=0").status_code == 400


def test_a_board_swap_is_one_at_a_time_and_changes_nothing(path: Path) -> None:
    node_info(path, 0, MAC_A, "First", counter=100, **A)
    position(path, 10, 52.1, counter=101, **A)
    node_info(path, 60, MAC_B, "Second", counter=500, **A)
    position(path, 70, 52.1, counter=501, **A)
    # The first board returns after RUN_GAP_MS: no run spans the second's report.
    node_info(path, 10 + 7 * 60, MAC_A, "First", counter=102, **A)
    assert refresh(path) == 0

    radios = get(path, f"/api/v1/nodes/{NODE}")["radios"]
    assert not radios["shared"]
    assert [(r["number"], r["long_name"], r["sorted_packets"]) for r in radios["items"]] == [
        (1, "First", None),
        (2, "Second", None),
    ]
    assert radios["unsorted_packets"] is None
    packets = get(path, f"/api/v1/nodes/{NODE}/packets")["packets"]
    assert {p["sender_radio"] for p in packets} == {None}
    conflict = get(path, "/api/v1/diagnostics/device-conflicts")["items"][0]
    assert (conflict["kind"], conflict["proof"]) == ("one_at_a_time", None)


def test_texts_numbered_by_the_phone_do_not_break_a_run(path: Path) -> None:
    node_info(path, 0, MAC_A, "Hilltop", counter=100, **A)
    position(path, 10, 52.1, counter=101, **A)
    for minute, counter in ((12, 400), (14, 800)):
        send(path, minute, TEXT, b"hi", counter=counter, **A)
    node_info(path, 20, MAC_B, "Valley", counter=700, **B)
    position(path, 40, 52.2, counter=102, **A)
    assert refresh(path) == 1


def test_diagnostics_shows_the_class_proof_and_other_numbers(path: Path) -> None:
    two_radios_at_once(path)
    node_info(path, -300, MAC_A, "Hilltop", counter=10, node=0x13579BDF, **A)
    refresh(path)

    conflict = get(path, "/api/v1/diagnostics/device-conflicts")["items"]
    item = next(c for c in conflict if c["node_id"] == NODE)
    assert item["kind"] == "shared"
    proof = item["proof"]
    minutes = {
        key: minute_of(get(path, f"/api/v1/packets/{proof[key]}")["packet"])
        for key in ("run_from_packet_row_id", "run_to_packet_row_id", "other_packet_row_id")
    }
    assert minutes == {
        "run_from_packet_row_id": 10,
        "run_to_packet_row_id": 40,
        "other_packet_row_id": 20,
    }
    by_mac = {report["mac_address"]: report for report in item["reports"]}
    assert by_mac["0a:1b:2c:3d:4e:5f"]["other_node_ids"] == [0x13579BDF]
    assert by_mac["f5:e4:d3:c2:b1:a0"]["other_node_ids"] == []


def test_earlier_and_later_numbers_need_reports_that_do_not_overlap(path: Path) -> None:
    old, other = 0x13579BDF, 0x12345678
    node_info(path, -300, MAC_A, "Hilltop", counter=10, node=old, **A)
    node_info(path, -200, MAC_A, "Hilltop", counter=11, node=old, **A)
    node_info(path, 0, MAC_A, "Hilltop", counter=100, **A)
    # The same MAC under another number while it reports here: no link.
    node_info(path, -10, MAC_B, "Valley", counter=5, node=other, **B)
    node_info(path, 10, MAC_B, "Valley", counter=700, **B)
    node_info(path, 20, MAC_B, "Valley", counter=6, node=other, **B)
    refresh(path)

    here = get(path, f"/api/v1/nodes/{NODE}")
    assert (here["earlier_node_ids"], here["later_node_ids"]) == ([old], [])
    before = get(path, f"/api/v1/nodes/{old}")
    assert (before["radios"], before["later_node_ids"]) == (None, [NODE])
    with closing(open_current_database(path)) as connection:
        connection.execute("INSERT INTO node_opt_out VALUES (?)", (old,))
    assert get(path, f"/api/v1/nodes/{NODE}")["earlier_node_ids"] == []


def test_a_radios_later_number_is_on_its_card(path: Path) -> None:
    node_info(path, 0, MAC_A, "Tower", counter=100, **A)
    node_info(path, 10, MAC_B, "Valley", counter=700, **B)
    node_info(path, 60, MAC_A, "Tower 2", counter=5, node=0x0BADF00D, **A)

    items = get(path, f"/api/v1/nodes/{NODE}")["radios"]["items"]
    assert [item["later_node_id"] for item in items] == [0x0BADF00D, None]


def test_retention_removes_sorted_rows_and_an_expired_proof_clears_the_class(
    path: Path, connection: sqlite3.Connection
) -> None:
    two_radios_at_once(path)
    # Each radio reports again, more than RUN_GAP_MS after its last packet.
    node_info(path, 600, MAC_A, "Hilltop", counter=104, **A)
    node_info(path, 700, MAC_B, "Valley", counter=702, **B)
    refresh_shared_numbers(connection)
    assert connection.execute("SELECT count(*) FROM node_shared").fetchone()[0] == 1

    apply_retention(connection, retention_days=1, now=START + timedelta(days=1, minutes=45))
    # The rows of deleted packets go with them: minutes 50, 600 and 700 are left.
    assert connection.execute("SELECT count(*) FROM packet_radio").fetchone()[0] == 3
    refresh_shared_numbers(connection)
    assert connection.execute("SELECT count(*) FROM node_shared").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM packet_radio").fetchone()[0] == 0
    radios = get(path, f"/api/v1/nodes/{NODE}")["radios"]
    assert not radios["shared"] and len(radios["items"]) == 2


def test_first_report_time_is_the_same_after_a_rebuild(
    path: Path, connection: sqlite3.Connection
) -> None:
    node_info(path, 5, MAC_A, "New name", counter=101, **A)
    # A late delivery of an older report keeps the newer names.
    node_info(path, 0, MAC_A, "Old name", counter=100, **A)
    query = "SELECT * FROM node_device ORDER BY node_id, mac_address, public_key"
    captured = [tuple(row) for row in connection.execute(query)]
    assert [(row[5], row[8]) for row in captured] == [
        ("New name", to_epoch_ms(START)),
    ]
    recreate_derived(connection)
    assert rederive(connection, lambda: False)
    assert [tuple(row) for row in connection.execute(query)] == captured


def test_no_public_response_names_a_mac(path: Path) -> None:
    two_radios_at_once(path)
    refresh(path)
    texts = [text for mac in (MAC_A, MAC_B) for text in (mac.hex(), mac.hex(":"))]
    texts += [base64.b64encode(mac).decode() for mac in (MAC_A, MAC_B)]
    since = START.isoformat().replace("+00:00", "Z")
    with TestClient(create_app(Settings(path))) as client:
        packet = client.get(f"/api/v1/nodes/{NODE}/packets").json()["packets"][0]
        urls = [
            f"/api/v1/nodes/{NODE}",
            f"/api/v1/nodes/{NODE}/packets",
            f"/api/v1/nodes/{NODE}/packets?radio=1",
            f"/api/v1/nodes/{NODE}/links?since={since}",
            f"/api/v1/nodes/{NODE}/contacts?since={since}",
            f"/api/v1/packets/{packet['packet_row_id']}",
            "/api/v1/packets",
            "/api/v1/map/nodes",
            "/api/v1/links?link_kind=direct",
        ]
        for url in urls:
            body = client.get(url).text
            assert not any(text in body for text in texts), url
        contact = client.get(f"/api/v1/nodes/{NODE}").json()["node"]["contact_url"]
        diagnostics = client.get("/api/v1/diagnostics/device-conflicts").text
    assert "0a:1b:2c:3d:4e:5f" in diagnostics
    encoded = contact.split("#", 1)[1]
    shared = admin_pb2.SharedContact.FromString(base64.urlsafe_b64decode(encoded + "=="))
    assert (shared.node_num, shared.user.long_name, shared.user.macaddr) == (NODE, "Valley", b"")


def test_a_shared_numbers_links_name_the_radios_that_made_them(path: Path) -> None:
    two_radios_at_once(path)
    refresh(path)
    since = (START - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    table = get(path, f"/api/v1/nodes/{NODE}/links?since={since}")
    names = {
        (
            row["link_kind"],
            row["node_a"] if row["node_b"] == NODE else row["node_b"],
            row["channel"],
        ): row["radio_names"]
        for row in table["items"]
    }
    assert names == {
        ("direct", GW_A, "MediumFast"): ["Hilltop"],
        ("direct", GW_A, "NarrowFast"): [],
        ("direct", GW_B, "NarrowFast"): ["Valley"],
        ("direct", GW_C, "MediumFast"): ["Hilltop"],
    }
    assert table["shared_node_ids"] == [NODE]
    # Other nodes' tables name no radios.
    other = get(path, f"/api/v1/nodes/{GW_A}/links?since={since}")
    assert {tuple(row["radio_names"]) for row in other["items"]} == {()}


def test_link_radio_names_follow_the_channel_and_hour_window(path: Path) -> None:
    two_radios_at_once(path)
    neighbour = 0xDDDDDDDD
    payload = mesh_pb2.NeighborInfo(
        node_id=NODE, neighbors=[mesh_pb2.Neighbor(node_id=neighbour, snr=5.0)]
    ).SerializeToString()
    for minute, mac, name, channel, gateway, counter in (
        (60, MAC_A, "Hilltop", "MediumFast", GW_A, 104),
        (70, MAC_B, "Valley", "NarrowFast", GW_B, 702),
        (120, MAC_B, "Valley", "MediumFast", GW_B, 704),
    ):
        node_info(path, minute, mac, name, counter=counter, gateway=GW_C, channel=channel)
        send(
            path,
            minute + 1,
            portnums_pb2.PortNum.NEIGHBORINFO_APP,
            payload,
            counter=counter + 1,
            gateway=gateway,
            channel=channel,
        )
    refresh(path)

    def names_since(minute: int) -> dict[tuple[str, str], list[str]]:
        since = (START + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
        table = get(path, f"/api/v1/nodes/{NODE}/links?since={since}")
        return {
            (row["link_kind"], row["channel"]): row["radio_names"]
            for row in table["items"]
            if {row["node_a"], row["node_b"]} & {GW_C, neighbour}
        }

    assert names_since(45) == {
        ("direct", "MediumFast"): ["Hilltop", "Valley"],
        ("direct", "NarrowFast"): ["Valley"],
        ("neighbor", "MediumFast"): ["Hilltop", "Valley"],
        ("neighbor", "NarrowFast"): ["Valley"],
    }
    # The table includes the whole cutoff hour, including minutes 120 and 121.
    assert names_since(130) == {
        ("direct", "MediumFast"): ["Valley"],
        ("neighbor", "MediumFast"): ["Valley"],
    }


# Placed nodes P and Q, the shared number S between them, and U, unplaced,
# heard only by S.
P, Q, S, U = 1, 2, 3, 4


def placed(connection: sqlite3.Connection, node: int, latitude: float, at: int) -> None:
    insert_row(
        connection,
        "node_current",
        node_id=node,
        last_seen_at=at,
        position_observed_at=at,
        latitude=latitude,
        longitude=21.0,
    )
    insert_row(connection, "node_position", node_id=node, at=at, latitude=latitude, longitude=21.0)


def shared_between(connection: sqlite3.Connection) -> int:
    at = to_epoch_ms(START)
    for node, latitude in ((P, 52.0), (Q, 52.05), (S, 52.1)):
        placed(connection, node, latitude, at)
    insert_row(connection, "node_current", node_id=U, last_seen_at=at)
    for a, b in ((P, Q), (S, P), (U, S)):
        for start, end in ((a, b), (b, a)):
            link_hour(connection, start, end, at + 60_000, km=5.0, snr=[5.0])
    insert_row(connection, "node_shared", node_id=S, proof_at=at, run_from_packet_row_id=1,
               run_to_packet_row_id=2, other_packet_row_id=3)  # fmt: skip
    connection.commit()
    return at + 60_000


def test_lines_from_a_shared_number_are_hidden(connection: sqlite3.Connection) -> None:
    heard = shared_between(connection)
    since = START - timedelta(hours=1)

    drawn = map_links(
        connection,
        link_kind="direct",
        since=since,
        limit=100,
        trusted=True,
    )
    assert [(link.node_a, link.node_b) for link in drawn.items] == [(P, Q)]
    table = link_table(connection, link_kinds=("direct",), since=since)
    assert {(row.node_a, row.node_b): row.doubts for row in table.items} == {
        (P, Q): (),
        (P, S): ("shared",),
        (S, U): ("shared",),
    }
    assert table.shared_node_ids == (S,)
    partners = direct_partners(connection, 0)
    assert set(partners) == {P, Q}
    assert set(partners[P].counted) == {Q}
    refresh_estimates(connection)
    assert connection.execute("SELECT count(*) FROM node_estimate").fetchone()[0] == 0
    assert own_reach(connection, {P, S}) == {P: 5.0}

    places = nodes_when_heard(connection, heard, {P, S})
    assert (places[S].latitude, places[P].latitude) == (None, 52.0)
    when = START + timedelta(minutes=1)
    found = positions_at(connection, (P, S), when)
    assert ([item.node_id for item in found.items], found.unplaced_node_ids) == ([P], (S,))


def test_without_the_shared_mark_the_same_links_count(connection: sqlite3.Connection) -> None:
    shared_between(connection)
    connection.execute("DELETE FROM node_shared")
    drawn = map_links(
        connection,
        link_kind="direct",
        since=START - timedelta(hours=1),
        limit=100,
        trusted=True,
    )
    assert len(drawn.items) == 3
    refresh_estimates(connection)
    assert [row[0] for row in connection.execute("SELECT node_id FROM node_estimate")] == [U]


def test_a_shared_numbers_contacts_have_no_distance(path: Path) -> None:
    with closing(open_current_database(path)) as connection:
        shared_between(connection)
    since = (START - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    contacts = get(path, f"/api/v1/nodes/{P}/contacts?since={since}")["items"]
    by_node = {item["node_id"]: item for item in contacts}
    assert by_node[Q]["km"] is not None
    assert by_node[S]["km"] is None
    assert {link["max_km"] for link in by_node[S]["links"]} == {None}
    assert json.dumps(by_node[Q]["links"]).count('"max_km": 5.0') == 1


def test_fallback_channels_follow_the_attributed_radio_and_refresh_with_it(
    path: Path,
    connection: sqlite3.Connection,
) -> None:
    send(path, -1, TEXT, b"early", counter=99, gateway=GW_B, channel="Test")
    two_radios_at_once(path)
    for minute, gateway in ((60, GW_A), (61, GW_B), (62, GW_C)):
        send(path, minute, TEXT, b"private", counter=980 + minute, gateway=gateway, channel="Test")
    refresh(path)

    def counts() -> dict[tuple[int, str], int]:
        return {
            (int(row[0]), str(row[1])): int(row[2])
            for row in connection.execute(
                """
                SELECT to_node_id, channel, sum(observations) FROM link_hour
                WHERE link_kind = 'direct' AND from_node_id = ? GROUP BY 1, 2
                """,
                (NODE,),
            )
        }

    first = counts()
    assert first[GW_B, "Unknown"] == 1
    assert first[GW_C, "Unknown"] == 1
    assert first[GW_A, "MediumFast"] == 5
    assert first[GW_B, "NarrowFast"] == 3
    since = START.isoformat().replace("+00:00", "Z")
    table = get(path, f"/api/v1/nodes/{NODE}/links?since={since}")
    assert next(
        r
        for r in table["items"]
        if r["channel"] == "MediumFast" and GW_A in (r["node_a"], r["node_b"])
    )["radio_names"] == ["Hilltop"]
    # A report by another node cannot identify which twin transmitted a hop.
    send(
        path,
        63,
        portnums_pb2.PortNum.NEIGHBORINFO_APP,
        mesh_pb2.NeighborInfo(
            neighbors=[mesh_pb2.Neighbor(node_id=NODE, snr=-3)]
        ).SerializeToString(),
        node=GW_A,
        counter=50,
        gateway=GW_B,
        channel="Test",
    )
    send(
        path,
        64,
        portnums_pb2.PortNum.TRACEROUTE_APP,
        mesh_pb2.RouteDiscovery(route=[NODE, GW_B], snr_towards=[12, 8]).SerializeToString(),
        node=GW_A,
        counter=51,
        gateway=GW_B,
        channel="Test",
    )
    # A new gateway association can sort an existing packet at the next refresh,
    # which recounts every link the shared number transmitted.
    node_info(path, 65, MAC_A, "Hilltop", counter=105, gateway=GW_C, channel="MediumFast")
    refresh(path)
    second = counts()
    assert (GW_C, "Unknown") not in second
    assert second[GW_C, "MediumFast"] == first[GW_C, "MediumFast"] + 2
    assert second[GW_B, "Unknown"] == 1
    assert {
        (row[0], row[1])
        for row in connection.execute(
            """
            SELECT link_kind, channel FROM link_hour
            WHERE from_node_id = ? AND link_kind != 'direct'
            """,
            (NODE,),
        )
    } == {("neighbor", "Unknown"), ("traceroute", "Unknown")}
    captured = [tuple(r) for r in connection.execute("SELECT * FROM link_hour ORDER BY 1,2,3,4,11")]
    connection.execute("UPDATE derived_state SET rebuilt = 0")
    assert rederive(connection, lambda: False, batch_size=2)
    assert [
        tuple(r) for r in connection.execute("SELECT * FROM link_hour ORDER BY 1,2,3,4,11")
    ] == captured


def test_relay_channel_updates_the_shared_number_without_attributing_a_radio(
    path: Path,
    connection: sqlite3.Connection,
) -> None:
    two_radios_at_once(path)
    refresh(path)
    before = [tuple(r) for r in connection.execute("SELECT * FROM packet_radio ORDER BY 1")]
    send(
        path,
        70,
        portnums_pb2.PortNum.TRACEROUTE_APP,
        mesh_pb2.RouteDiscovery(route=[NODE], snr_towards=[12]).SerializeToString(),
        node=GW_A,
        counter=55,
        gateway=GW_B,
        channel="LongSlow",
    )
    assert (
        connection.execute(
            "SELECT channel FROM node_current WHERE node_id = ?",
            (NODE,),
        ).fetchone()[0]
        == "LongSlow"
    )
    refresh(path)
    assert [tuple(r) for r in connection.execute("SELECT * FROM packet_radio ORDER BY 1")] == before
