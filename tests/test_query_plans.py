"""The query plans SQLite picks for capture, retention and every page's requests.

SigSleuth never runs ANALYZE, so with the same SQLite version the plan on this
tiny database is the plan on a live one with tens of millions of receptions.
A new index can quietly move another page onto it (packet_port once sent Chat
and the node page through every packet of a port), so the plans are kept in
query_plans.txt and any change shows up as a diff to look at. Only table
scans and searches and their indexes are compared. SQLite's version-dependent
plan tree and annotations are omitted.

After an intended change, rewrite the file and read its diff:

    UPDATE_QUERY_PLANS=1 uv run --locked pytest tests/test_query_plans.py

If ANALYZE or PRAGMA optimize is ever added, plans start depending on the data
and this test no longer stands for the live database.
"""

import re
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import (  # type: ignore[import-untyped]
    mesh_pb2,
    portnums_pb2,
    telemetry_pb2,
)

import sigsleuth
from derived_rows import link_hour
from meshtastic_fixtures import application_envelope
from page_requests import Subjects, page_requests
from sigsleuth import api
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database, open_database, to_epoch_ms
from sigsleuth.ingest import MqttDelivery, ingest_delivery
from sigsleuth.positions import refresh_estimates
from sigsleuth.retention import apply_retention
from snapshot_file import check_snapshot

PLANS_FILE = Path(__file__).with_name("query_plans.txt")
PACKAGE_DIR = str(Path(sigsleuth.__file__).parent)
# Tables that grow with traffic and retention. A full scan of one is slow on
# a live database unless the statement is bounded some other way; those
# statements are listed in ALLOWED_SCANS with the reason.
BIG_TABLES = {
    "packet",
    "reception",
    "link_hour",
    "hop_cost_hour",
    "node_activity_hour",
    "traceroute_hop",
    "traceroute_packet",
    "coverage_measurement",
}
# Measured 2026-10-08 on a local loranet.pl capture (228,000 packets, 2.7
# million receptions), warm cache. Revisit if a page that runs one is slow.
ALLOWED_SCANS = {
    # Counts every packet and reception (18 ms for the receptions); the
    # operator page, not one browsed often. The other scans are LIMIT 1.
    "diagnostics.diagnostics_summary": "operator page totals",
    # Reads the link_hour_pair index, not the table: 56 ms on 14 days of
    # loranet.pl (2026-10-12).
    "diagnostics.longest_links": "operator page, long links",
    # Texts are about 2% of packets; the partial text index scans in 0.3 ms.
    "chat.chat_channels": "text messages only",
}
REQUESTER, RELAY, TARGET = 0x0A0A0A0A, 0x0B0B0B0B, 0x0C0C0C0C
# A text the gateways heard through RELAY, for the packet's relay routes.
RELAYED_TEXT = 90
GATEWAYS = ("!aaaaaaaa", "!bbbbbbbb")


class Statements:
    """Collects each traced statement with the SigSleuth function that ran it."""

    def __init__(self) -> None:
        self.section = ""
        self.seen: list[tuple[str, str, str]] = []

    def watch(self, connection: sqlite3.Connection) -> sqlite3.Connection:
        connection.set_trace_callback(self._record)
        return connection

    def _record(self, sql: str) -> None:
        if sql.lstrip().upper().startswith(("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")):
            self.seen.append((self.section, _caller(), sql))


def _caller() -> str:
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_filename.startswith(PACKAGE_DIR):
            module = Path(frame.f_code.co_filename).stem
            return f"{module}.{frame.f_code.co_qualname}"
        frame = frame.f_back  # type: ignore[assignment]
    return "?"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    path = tmp_path / "history.db"
    migrate_database(path)
    return path


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> Statements:
    recorded = Statements()
    monkeypatch.setattr(
        api,
        "open_current_database",
        lambda database_path: recorded.watch(open_current_database(database_path)),
    )
    return recorded


def test_query_plans_match_the_saved_ones(path: Path, statements: Statements) -> None:
    now = datetime.now(UTC)
    statements.section = "capture"
    with closing(statements.watch(open_current_database(path))) as connection:
        position = mesh_pb2.Position(latitude_i=521000000, longitude_i=49000000)
        envelope = application_envelope(
            portnums_pb2.PortNum.POSITION_APP,
            position.SerializeToString(),
            sender=RELAY + 0x100,
            bitfield=1,
            gateway_id=GATEWAYS[0],
        )
        ingest_delivery(
            connection,
            MqttDelivery(
                to_epoch_ms(now - timedelta(days=3)),
                f"msh/EU_868/2/e/LongFast/{GATEWAYS[0]}",
                envelope,
            ),
        )
        capture_some_history(connection, now - timedelta(minutes=30))
    with closing(open_database(path)) as connection:
        (relayed_text,) = connection.execute(
            "SELECT id FROM packet WHERE packet_id = ?", (RELAYED_TEXT,)
        ).fetchone()
        (request,) = connection.execute(
            "SELECT min(packet_row_id) FROM traceroute_packet WHERE NOT is_reply"
        ).fetchone()
        (relayed_reception,) = connection.execute(
            """
            SELECT min(r.id) FROM reception AS r JOIN packet AS t ON t.id = r.packet_row_id
            WHERE r.packet_row_id = ? AND r.relay_byte > 0 AND r.hop_start > r.hop_limit
              AND r.relay_byte NOT IN (r.gateway_node_id & 255, t.sender_node_id & 255)
            """,
            (relayed_text,),
        ).fetchone()

    settings = Settings(path, default_preset="LongFast", upload_password="survey")
    client = TestClient(create_app(settings), headers={"Authorization": "Basic cGhvbmU6c3VydmV5"})
    statements.section = "POST /api/v1/coverage/uploads"
    response = client.post("/api/v1/coverage/uploads", json=survey_upload(now))
    assert response.status_code == 200, response.text
    subjects = Subjects(
        REQUESTER,
        RELAY,
        name="hill",
        channel="LongFast",
        text_sender=REQUESTER,
        flood_packet=relayed_text,
        traceroute_packet=request,
        relayed_reception=relayed_reception,
    )
    pages = page_requests(now, subjects)
    for url in chain(pages, coverage_requests(client, now)):
        # The windows' start times change with every run; the plans do not.
        statements.section = "GET " + re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+Z", "<time>", url)
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)

    statements.section = "position corroboration"
    with closing(statements.watch(open_current_database(path))) as connection:
        link_hour(connection, REQUESTER, RELAY, to_epoch_ms(now), km=200.0)
        refresh_estimates(connection)

    statements.section = "retention and estimates"
    with closing(statements.watch(open_current_database(path))) as connection:
        # Far enough ahead that everything is old and every delete runs.
        apply_retention(connection, retention_days=14, now=now + timedelta(days=60))
        refresh_estimates(connection)

    with closing(open_database(path)) as connection:
        plans = explain(connection, statements.seen)

    scans = [
        f"{section}: {line.strip()}"
        for section, entries in plans.items()
        for caller, plan in entries
        for line in plan
        if scanned_table(line) in BIG_TABLES and caller not in ALLOWED_SCANS
    ]
    check_snapshot(PLANS_FILE, render(plans), "UPDATE_QUERY_PLANS")
    assert not scans, "Full scans of big tables:\n" + "\n".join(scans)


def capture_some_history(connection: sqlite3.Connection, start: datetime) -> None:
    """A little of everything the pages read: names, positions, telemetry, texts
    with a reply, neighbour info, a traceroute with its reply, one whose route
    pins a free hop, and a text relayed by a router the gateways heard
    directly, each heard by two gateways."""
    deliveries: list[tuple[int, bytes, dict[str, float], dict[str, float]]] = []

    def send(
        portnum: int, payload: bytes, second: dict[str, float] | None = None, **fields: float
    ) -> None:
        """second: what the second gateway heard otherwise."""
        deliveries.append((portnum, payload, fields, second or {}))

    for node, latitude in ((REQUESTER, 52.0), (RELAY, 52.1), (TARGET, 52.2)):
        position = mesh_pb2.Position(latitude_i=int(latitude * 1e7), longitude_i=int(4.9e7))
        send(portnums_pb2.PortNum.POSITION_APP, position.SerializeToString(), sender=node)
    user = mesh_pb2.User(long_name="Hilltop", short_name="HILL", hw_model=9)
    send(portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString(), sender=REQUESTER)
    router = mesh_pb2.User(long_name="Mast", short_name="MAST", role=2)  # ROUTER
    send(portnums_pb2.PortNum.NODEINFO_APP, router.SerializeToString(), sender=RELAY)
    metrics = telemetry_pb2.Telemetry(
        device_metrics=telemetry_pb2.DeviceMetrics(channel_utilization=12.5, air_util_tx=1.5)
    )
    # Heard directly: no hops used, the sender's own relay byte.
    send(
        portnums_pb2.PortNum.TELEMETRY_APP,
        metrics.SerializeToString(),
        sender=REQUESTER,
        hop_start=3,
        hop_limit=3,
        relay_node=REQUESTER & 0xFF,
        rx_snr=6.0,
    )
    send(portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello", sender=REQUESTER, packet_id=50)
    send(portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hi", sender=TARGET, reply_id=50)
    neighbors = mesh_pb2.NeighborInfo(
        node_id=RELAY, neighbors=[mesh_pb2.Neighbor(node_id=TARGET, snr=-3.5)]
    )
    send(portnums_pb2.PortNum.NEIGHBORINFO_APP, neighbors.SerializeToString(), sender=RELAY)
    request = mesh_pb2.RouteDiscovery(route=[RELAY], snr_towards=[20])
    send(
        portnums_pb2.PortNum.TRACEROUTE_APP,
        request.SerializeToString(),
        sender=REQUESTER,
        recipient=TARGET,
        packet_id=70,
    )
    reply = mesh_pb2.RouteDiscovery(
        route=[RELAY], snr_towards=[20, 16], route_back=[RELAY], snr_back=[12, 8]
    )
    send(
        portnums_pb2.PortNum.TRACEROUTE_APP,
        reply.SerializeToString(),
        sender=TARGET,
        recipient=REQUESTER,
        packet_id=71,
        request_id=70,
    )
    # One hop used over two relays: TARGET passed on RELAY's copy for free.
    send(
        portnums_pb2.PortNum.TRACEROUTE_APP,
        mesh_pb2.RouteDiscovery(route=[RELAY, TARGET], snr_towards=[20, 12]).SerializeToString(),
        sender=REQUESTER,
        recipient=0x0D0D0D0D,
        hop_start=3,
        hop_limit=2,
        relay_node=TARGET & 0xFF,
    )
    # The requester then moves a little, so pages read where it was before.
    moved = mesh_pb2.Position(latitude_i=int(52.01e7), longitude_i=int(4.9e7))
    send(portnums_pb2.PortNum.POSITION_APP, moved.SerializeToString(), sender=REQUESTER)
    send(
        portnums_pb2.PortNum.TELEMETRY_APP,
        metrics.SerializeToString(),
        sender=RELAY,
        hop_start=3,
        hop_limit=3,
        relay_node=RELAY & 0xFF,
        rx_snr=4.0,
    )
    send(
        portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        b"relayed",
        sender=TARGET,
        packet_id=RELAYED_TEXT,
        hop_start=3,
        hop_limit=1,
        relay_node=RELAY & 0xFF,
        rx_snr=-2.0,
        # After one hop there, so route inference asks about the router at both.
        second={"hop_limit": 2},
    )

    for number, (portnum, payload, fields, second) in enumerate(deliveries, start=1):
        # OK to MQTT on: refreshes whether the sender opted out, hiding none.
        header = {"packet_id": number, "bitfield": 1, **fields}
        for gateway in GATEWAYS:
            heard = header if gateway == GATEWAYS[0] else {**header, **second}
            envelope = application_envelope(portnum, payload, gateway_id=gateway, **heard)  # type: ignore[arg-type]
            received_at = to_epoch_ms(start + timedelta(seconds=number))
            topic = f"msh/EU_868/2/e/LongFast/{gateway}"
            ingest_delivery(connection, MqttDelivery(received_at, topic, envelope))


def survey_upload(now: datetime) -> dict[str, object]:
    session = {
        "id": "8bebbf05-be4e-47e4-abcb-b845af2aeb90",
        "receiver_node_id": 47203607,
        "receiver_name": "Survey node",
        "modem_preset": "LongFast",
        "app_version": "2.8.3-survey.1",
    }
    measurement = {
        "seq": 1,
        "heard_at_ms": to_epoch_ms(now) - 60_000,
        "latitude": 52.05,
        "longitude": 4.9,
        "accuracy_m": 14.4,
        "fix_age_ms": 701,
        "sender_node_id": REQUESTER,
        "recipient_node_id": 0xFFFFFFFF,
        "packet_id": 1,
        "rx_snr": 12.75,
        "rx_rssi": -82,
        "hop_start": 3,
        "hop_limit": 2,
        "relay_byte": RELAY & 0xFF,
        "ok_to_mqtt": True,
    }
    # RELAY heard directly names the relay of the first (a survey link).
    direct = {
        **measurement,
        "seq": 2,
        "sender_node_id": RELAY,
        "packet_id": 2,
        "hop_start": 3,
        "hop_limit": 3,
    }
    unknown = {**measurement, "seq": 3, "latitude": 53.05}
    return {"session": session, "measurements": [measurement, direct, unknown]}


def coverage_requests(client: TestClient, now: datetime) -> Iterator[str]:
    """The coverage layer's requests, around the survey upload."""
    day = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    area = "west=4&south=51&east=6&north=53&zoom=12"
    yield f"/api/v1/coverage/cells?{area}"
    yield f"/api/v1/coverage/cells?{area}&since={day}&node={REQUESTER}&channel=LongFast"
    yield f"/api/v1/coverage/extent?node={REQUESTER}"
    yield f"/api/v1/coverage/extent?node={REQUESTER}&channel=LongFast"
    yield "/api/v1/coverage/nodes?west=4&south=51&east=6&north=53"
    yield "/api/v1/coverage/relay-nodes?spreading_factor=11"
    cells = client.get(f"/api/v1/coverage/cells?{area}").json()
    cell = cells["cells"][0]
    yield f"/api/v1/coverage/cells/{cells['level']}/{cell['x']}/{cell['y']}"
    yield f"/api/v1/coverage/cells/{cells['level']}/{cell['x']}/{cell['y']}?node={REQUESTER}"


Plans = dict[str, list[tuple[str, list[str]]]]


def test_snapshot_ignores_annotations_but_detects_index_loss() -> None:
    # Disabling the statement cache makes EXPLAIN recompile after DROP INDEX.
    with closing(sqlite3.connect(":memory:", cached_statements=0)) as connection:
        connection.executescript(
            """
            CREATE TABLE packet (id INTEGER PRIMARY KEY, sender_node_id INTEGER);
            CREATE INDEX packet_sender ON packet (sender_node_id);
            """
        )
        sql = "SELECT id FROM packet WHERE sender_node_id = 1"
        annotated = "SELECT id, (SELECT 1) FROM packet AS p WHERE sender_node_id = 1"
        indexed = explain(connection, [("request", "page", sql)])
        assert render(indexed) == render(explain(connection, [("request", "page", annotated)]))

        connection.execute("DROP INDEX packet_sender")
        unindexed = explain(connection, [("request", "page", sql)])
        assert render(indexed) != render(unindexed)
        assert any(
            scanned_table(line) in BIG_TABLES for _, lines in unindexed["request"] for line in lines
        )


def explain(connection: sqlite3.Connection, seen: list[tuple[str, str, str]]) -> Plans:
    """Each section's distinct table accesses, with the function that ran them.

    SQLite releases change plan-tree structure, subquery IDs, bloom filters,
    covering-index annotations and search-key spelling (id versus rowid).
    Keep the scan/index choices that matter on large tables, without those
    incidental details.
    """
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    plans: Plans = {}
    for section, caller, sql in seen:
        rows = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        aliases = table_aliases(sql)
        lines = []
        for _, _, _, detail in rows:
            match = re.match(r"^(SCAN|SEARCH) (\w+)(.*)", detail)
            if match is None:
                continue
            operation, name, access = match.groups()
            table = aliases.get(name, name)
            if table not in tables:
                continue
            # Keep the index name, but not key predicates or annotations.
            # COVERING annotations for expression indexes also differ
            # between SQLite versions.
            index = re.search(r"USING (?:COVERING )?INDEX (\w+)", access)
            if index is not None:
                access = " USING INDEX " + index.group(1)
            elif "PRIMARY KEY" in access:
                access = " USING PRIMARY KEY"
            elif "AUTOMATIC" in access:
                access = " USING AUTOMATIC INDEX"
            else:
                access = ""
            lines.append(f"{operation} {table}{access}")
        entry = (caller, lines)
        if lines and entry not in plans.setdefault(section, []):
            plans[section].append(entry)
    return plans


def table_aliases(sql: str) -> dict[str, str]:
    """`FROM packet AS t` or `JOIN packet t` → {"t": "packet"}."""
    found = re.findall(r"\b(?:FROM|JOIN)\s+(\w+)\s+(?:AS\s+)?(\w+)", sql, re.IGNORECASE)
    keywords = {"where", "join", "on", "left", "inner", "cross", "indexed", "group", "order"}
    return {alias: table for table, alias in found if alias.lower() not in keywords}


def scanned_table(line: str) -> str | None:
    match = re.match(r"\s*SCAN (?:\w+=)?(\w+)", line)
    return match.group(1) if match else None


def render(plans: Plans) -> str:
    blocks = []
    for section, entries in plans.items():
        body = "\n".join(
            caller + "\n" + "\n".join("  " + line for line in plan) for caller, plan in entries
        )
        blocks.append(f"## {section}\n\n{body}\n")
    return "\n".join(blocks)
