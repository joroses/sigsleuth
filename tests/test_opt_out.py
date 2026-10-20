"""Nodes that turned OK to MQTT off: which they are, and that no page serves one."""

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest
from page_requests import Subjects, page_requests, pick_subjects
from recorded_sample import Sample
from sigsleuth import __main__ as commands
from sigsleuth import db, opt_out
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import (
    from_epoch_ms,
    migrate_database,
    open_current_database,
    recreate_derived,
    to_epoch_ms,
    transaction,
)
from sigsleuth.decode import read_neighbors
from sigsleuth.derive import rederive
from sigsleuth.opt_out import unshown_packets
from sigsleuth.retention import apply_retention

NODE, GATEWAY, HIDDEN = 0x0D0D0D0D, 0xAAAAAAAA, 0x0E0E0E0E
START = datetime(2026, 10, 1, 9, tzinfo=UTC)
DAY = 24 * 60


def send(
    path: Path,
    minute: int,
    bitfield: int | None,
    *,
    portnum: int = portnums_pb2.PortNum.TEXT_MESSAGE_APP,
    payload: bytes = b"hello",
    gateway: int = GATEWAY,
    sender: int = NODE,
    recipient: int = 0xFFFFFFFF,
) -> None:
    envelope = application_envelope(
        portnum,
        payload,
        sender=sender,
        recipient=recipient,
        packet_id=minute + 1,
        bitfield=bitfield,
        gateway_id=f"!{gateway:08x}",
    )
    ingest(
        path,
        envelope,
        received_at=START + timedelta(minutes=minute),
        topic=f"msh/EU_868/2/e/LongFast/!{gateway:08x}",
    )


def shown(client: TestClient) -> bool:
    """Whether the node's page and the node list show the node."""
    node = client.get(f"/api/v1/nodes/{NODE}").json()["node"]
    listed = [item["node_id"] for item in client.get("/api/v1/nodes").json()["items"]]
    assert (node is not None) == (NODE in listed)
    return node is not None


def test_a_node_is_hidden_while_its_newest_packet_says_ok_to_mqtt_off(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    user = mesh_pb2.User(long_name="Quiet Hill", short_name="QH")
    send(path, 0, 0, portnum=portnums_pb2.PortNum.NODEINFO_APP, payload=user.SerializeToString())
    client = TestClient(create_app(Settings(path, upload_password="survey")))
    assert not shown(client)
    assert client.get("/api/v1/chat").json()["messages"] == []

    # A report without the bit changes nothing; one with it on shows the
    # node's whole history again, its name too.
    send(path, 1, None)
    assert not shown(client)
    send(path, 2, 1)
    assert shown(client)
    assert client.get(f"/api/v1/nodes/{NODE}").json()["node"]["long_name"] == "Quiet Hill"
    assert len(client.get("/api/v1/chat").json()["messages"]) == 2
    send(path, 3, 0)
    assert not shown(client)

    # A survey's newer packet decides as well as a captured one.
    survey = {
        "session": {"id": "a", "receiver_node_id": 0x0F0F0F0F},
        "measurements": [
            {
                "seq": 1,
                "heard_at_ms": to_epoch_ms(START + timedelta(minutes=4)),
                "latitude": 52.0,
                "longitude": 4.9,
                "sender_node_id": NODE,
                "ok_to_mqtt": True,
            }
        ],
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "fixed_now_ms", to_epoch_ms(START + timedelta(minutes=5)))
        response = client.post("/api/v1/coverage/uploads", json=survey, auth=("phone", "survey"))
        assert response.json()["saved"] == 1
    assert shown(client)

    # Rebuilt from the saved packets, it is decided as capture decided it.
    send(path, 6, 0)
    rebuild(path)
    assert not shown(client)

    # A node that reports to MQTT itself is never hidden.
    send(path, 7, 0, gateway=NODE)
    assert shown(client)
    rebuild(path)
    assert shown(client)


def test_hiding_turned_off_shows_every_node_and_startup_follows_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    send(path, 0, 0)
    client = TestClient(create_app(Settings(path)))
    assert not shown(client)
    # Restored after the test; main sets it.
    monkeypatch.setattr(opt_out, "hiding", True)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_options: None)

    def start(hide: str) -> None:
        environment = {"SIGSLEUTH_DATABASE_PATH": str(path), "SIGSLEUTH_HIDE_OPTED_OUT": hide}
        monkeypatch.setattr(commands, "load_environment", lambda: environment)
        assert commands.main(["api"]) == 0

    start("false")
    assert shown(client)
    # Capture hides no one either.
    send(path, 1, 0)
    assert shown(client)
    start("true")
    assert not shown(client)


def test_a_shown_nodes_packets_to_an_opted_out_one_keep_their_times_but_no_links(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    send(path, 0, 0, sender=HIDDEN)
    # NODE answers HIDDEN's requests: its name, its position, then a text.
    user = mesh_pb2.User(long_name="Answering Hill", short_name="AH")
    position = mesh_pb2.Position(latitude_i=520_000_000, longitude_i=49_000_000)
    for minute, portnum, payload in (
        (1, portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString()),
        (2, portnums_pb2.PortNum.POSITION_APP, position.SerializeToString()),
        (3, portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hi"),
    ):
        send(path, minute, 1, portnum=portnum, payload=payload, recipient=HIDDEN)
    client = TestClient(create_app(Settings(path)))

    def links() -> list[Any]:
        node = client.get(f"/api/v1/nodes/{NODE}").json()["node"]
        (listed,) = [n for n in client.get("/api/v1/nodes").json()["items"] if n["node_id"] == NODE]
        (drawn,) = client.get("/api/v1/map/nodes").json()["items"]
        history = client.get(f"/api/v1/nodes/{NODE}/positions").json()["items"]
        assert node["last_sent_at"] == listed["last_sent_at"] == drawn["last_sent_at"]
        assert not node["position"]["from_map_report"]
        return [
            node["last_sent_packet_row_id"],
            node["node_info_packet_row_id"],
            node["position"]["packet_row_id"],
            listed["last_sent_packet_row_id"],
            drawn["last_sent_packet_row_id"],
            drawn["position"]["packet_row_id"],
            history[0]["packet_row_id"],
        ]

    assert links() == [None] * 7
    assert client.get("/api/v1/packets/4").status_code == 404
    send(path, 4, 1, sender=HIDDEN)
    assert links() == [4, 2, 3, 4, 4, 3, 3]


def rebuild(path: Path) -> None:
    with closing(open_current_database(path)) as connection:
        recreate_derived(connection)
        assert rederive(connection, lambda: False)


# Fields that hold packet IDs, which may equal a node number by chance.
PACKET_ID_FIELDS = {"packet_id", "request_packet_id", "reply_id", "request_id"}


def leaks(value: Any, hidden: set[int], where: str = "") -> Iterator[str]:
    """Where a response names a hidden node: as a number, or inside text
    (a payload, a search) as a number or !hex."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in PACKET_ID_FIELDS:
                yield from leaks(item, hidden, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaks(item, hidden, f"{where}[{index}]")
    elif isinstance(value, int) and not isinstance(value, bool) and value in hidden:
        yield where
    elif isinstance(value, str) and not numbers_in(value).isdisjoint(hidden):
        yield where


def packet_links(value: Any) -> Iterator[int]:
    """The packet row IDs a response links to."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("packet_row_id") and isinstance(item, int):
                yield item
            else:
                yield from packet_links(item)
    elif isinstance(value, list):
        for item in value:
            yield from packet_links(item)


def numbers_in(text: str) -> set[int]:
    """The decimal and !hex numbers in text that could be node numbers."""
    numbers = {int(found) for found in re.findall(r"\d{4,10}", text)}
    return numbers | {int(found, 16) for found in re.findall(r"!([0-9a-fA-F]{8})", text)}


@pytest.fixture
def hidden_sample(
    sample: Sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Subjects, set[int]]]:
    """A copy of the recorded sample, read a minute after its last delivery,
    in which half of the nodes that are not gateways opted out: those whose
    number's parity differs from the subject node's, so its pages show a
    visible node among hidden ones."""
    monkeypatch.setattr(db, "fixed_now_ms", sample.now_ms)
    path = tmp_path / "hidden.db"
    with closing(sqlite3.connect(sample.path)) as source, closing(sqlite3.connect(path)) as copy:
        source.backup(copy)
    subjects = pick_subjects(sample.path)
    with closing(open_current_database(path)) as connection:
        connection.execute(
            """
            INSERT INTO node_opt_out (node_id)
            SELECT node_id FROM node_current
            WHERE last_gateway_at IS NULL AND node_id % 2 != ?
            """,
            (subjects.node % 2,),
        )
        hidden = {int(row[0]) for row in connection.execute("SELECT node_id FROM node_opt_out")}
    yield path, subjects, hidden


def test_no_page_names_a_node_that_opted_out(
    sample: Sample,
    hidden_sample: tuple[Path, Subjects, set[int]],
) -> None:
    path, subjects, hidden = hidden_sample
    client = TestClient(create_app(Settings(path, default_preset="LongFast")))
    with closing(open_current_database(path)) as connection:
        extra = [f"/api/v1/packets/{packet}" for packet in masked_packets(connection, hidden)]
        nodes = connection.execute("SELECT count(*) FROM node_current").fetchone()[0]
    # The packets whose routes or neighbour lists must hide someone.
    assert len(extra) >= 4

    linked: set[int] = set()
    for url in [*page_requests(from_epoch_ms(sample.now_ms), subjects), *extra]:
        response = client.get(url)
        if response.status_code == 404 and url.startswith("/api/v1/packets/"):
            continue  # a hidden node's packet
        assert response.status_code == 200, (url, response.text)
        # A node's own requests repeat its number, as for a node never heard.
        assert list(leaks(response.json(), hidden - numbers_in(url))) == [], url
        linked |= set(packet_links(response.json()))

    # No link opens a packet that is not shown.
    with closing(open_current_database(path)) as connection:
        assert unshown_packets(connection, linked) == set()

    # The other nodes all show.
    listed = client.get("/api/v1/nodes", params={"limit": 1}).json()["total"]
    assert listed == nodes - len(hidden)


def masked_packets(connection: sqlite3.Connection, hidden: set[int]) -> list[int]:
    """Up to three shown traceroutes with a hidden node on a route, and up to
    three shown NeighborInfo packets listing one."""
    routes = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT tr.packet_row_id FROM traceroute_packet AS tr
            WHERE requester_node_id NOT IN (SELECT node_id FROM node_opt_out)
              AND target_node_id NOT IN (SELECT node_id FROM node_opt_out)
              AND EXISTS (
                  SELECT 1 FROM traceroute_hop AS h
                  WHERE h.packet_row_id = tr.packet_row_id
                    AND (h.from_node_id IN (SELECT node_id FROM node_opt_out)
                         OR h.to_node_id IN (SELECT node_id FROM node_opt_out))
              )
            ORDER BY tr.packet_row_id LIMIT 3
            """
        )
    ]
    neighbours = [
        int(row["id"])
        for row in connection.execute(
            """
            SELECT id, application_payload FROM packet
            WHERE portnum = 71 AND application_payload IS NOT NULL
              AND sender_node_id NOT IN (SELECT node_id FROM node_opt_out)
            ORDER BY id
            """
        )
        if any(n.node_id in hidden for n in read_neighbors(bytes(row["application_payload"])))
    ][:3]
    return routes + neighbours


def test_an_opted_out_node_reads_as_one_never_heard(
    sample: Sample,
    hidden_sample: tuple[Path, Subjects, set[int]],
) -> None:
    path, subjects, hidden = hidden_sample
    client = TestClient(create_app(Settings(path, default_preset="LongFast")))
    with closing(open_current_database(path)) as connection:
        # The hidden node in the most traceroute hops, and one of its packets.
        node, packet = connection.execute(
            """
            SELECT h.from_node_id, min(p.id) FROM traceroute_hop AS h
            JOIN packet AS p ON p.sender_node_id = h.from_node_id
            WHERE h.from_node_id IN (SELECT node_id FROM node_opt_out)
            GROUP BY h.from_node_id ORDER BY count(*) DESC LIMIT 1
            """
        ).fetchone()
    never = 0x7E7E7E7E
    week = from_epoch_ms(sample.now_ms - 7 * 86_400_000).isoformat().replace("+00:00", "Z")
    other, channel = subjects.node, subjects.channel

    def urls(n: int) -> list[str]:
        return [
            f"/api/v1/nodes/{n}",
            f"/api/v1/nodes/{n}/packets?limit=50",
            f"/api/v1/nodes/{n}/charts?since={week}",
            f"/api/v1/nodes/{n}/positions",
            f"/api/v1/nodes/{n}/contacts?since={week}",
            f"/api/v1/nodes/{n}/links?since={week}",
            f"/api/v1/map/text-reach/{n}?since={week}",
            f"/api/v1/packets?limit=50&sender={n}",
            f"/api/v1/nodes?q=!{n:08x}",
            f"/api/v1/traceroutes?node=!{n:08x}",
            f"/api/v1/traceroutes/hops?node_a={n}&node_b={other}",
            f"/api/v1/links/hours?node_a={n}&node_b={other}&channel={channel}",
            f"/api/v1/links/positions?node_a={n}&node_b={other}&at={week}",
            f"/api/v1/diagnostics/longest-links?node={n}",
        ]

    for hidden_url, never_url in zip(urls(node), urls(never), strict=True):
        answer = client.get(hidden_url)
        as_never = json.dumps(client.get(never_url).json()).replace(str(never), str(node))
        assert answer.status_code == 200
        assert json.dumps(answer.json()) == as_never, hidden_url
    assert node in hidden
    missing = client.get("/api/v1/packets/999999999")
    assert client.get(f"/api/v1/packets/{packet}").json() == missing.json()


def test_retention_keeps_an_opted_out_node_hidden_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    user = mesh_pb2.User(long_name="Quiet Hill", short_name="QH")
    # Day 0: NodeInfo with OK to MQTT on, which node_current keeps naming.
    # Day 1: a text with it off, then packets that do not tell it.
    send(path, 0, 1, portnum=portnums_pb2.PortNum.NODEINFO_APP, payload=user.SerializeToString())
    send(path, DAY, 0)
    send(path, DAY + 60, None)
    send(path, 15 * DAY, None)
    client = TestClient(create_app(Settings(path)))
    assert not shown(client)
    with closing(open_current_database(path)) as connection:
        removed = apply_retention(connection, retention_days=14, now=START + timedelta(days=16))
        # What startup does (__main__._refresh_opt_outs).
        with transaction(connection):
            opt_out.refresh_opt_outs(connection, None)
    # Retention keeps the newest packet with the bit off, which decides.
    assert removed.packets == 1
    assert not shown(client)


def test_retention_still_deletes_with_a_node_hidden_only_by_a_survey(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    send(path, 0, 1)
    send(path, 15 * DAY, 1)
    client = TestClient(create_app(Settings(path, upload_password="survey")))
    # HIDDEN has no packet with the bit off, only a survey measurement.
    survey = {
        "session": {"id": "a", "receiver_node_id": 0x0F0F0F0F},
        "measurements": [
            {
                "seq": 1,
                "heard_at_ms": to_epoch_ms(START + timedelta(days=15)),
                "latitude": 52.0,
                "longitude": 4.9,
                "sender_node_id": HIDDEN,
                "ok_to_mqtt": False,
            }
        ],
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "fixed_now_ms", to_epoch_ms(START + timedelta(days=15, minutes=1)))
        response = client.post("/api/v1/coverage/uploads", json=survey, auth=("phone", "survey"))
        assert response.json()["saved"] == 1
    with closing(open_current_database(path)) as connection:
        assert opt_out.is_opted_out(connection, HIDDEN)
        removed = apply_retention(connection, retention_days=14, now=START + timedelta(days=16))
    assert removed.packets == 1
