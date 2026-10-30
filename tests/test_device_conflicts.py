import base64
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, open_current_database, recreate_derived
from sigsleuth.derive import rederive
from sigsleuth.retention import apply_retention
from sigsleuth.shared_numbers import refresh_shared_numbers

START = datetime(2026, 10, 1, tzinfo=UTC)
NODE = 0x11111111
KEY = bytes(range(32))
MAC_A, MAC_B = bytes.fromhex("112233445566"), bytes.fromhex("aabbccddeeff")
URL = "/api/v1/diagnostics/device-conflicts"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    result = tmp_path / "history.db"
    migrate_database(result)
    return result


def send(path: Path, minute: int, *, node: int = NODE, **fields: Any) -> None:
    user = mesh_pb2.User(long_name=fields.pop("long_name", "Hilltop"), **fields)
    ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.NODEINFO_APP,
            user.SerializeToString(),
            sender=node,
            packet_id=minute + 1,
        ),
        received_at=START + timedelta(minutes=minute),
    )


def page(path: Path, suffix: str = "") -> dict[str, Any]:
    return TestClient(create_app(Settings(path))).get(URL + suffix).json()  # type: ignore[no-any-return]


def test_conflicts_keep_historical_names_and_latest_evidence_per_profile(path: Path) -> None:
    send(path, 0, macaddr=MAC_A, public_key=KEY, long_name="Old radio")
    send(path, 2, macaddr=MAC_B, public_key=KEY, long_name="Other radio")
    send(path, 4, macaddr=MAC_A, public_key=KEY, long_name="Renamed radio")
    # An old import arriving later must not replace the profile's latest report.
    send(path, 1, macaddr=MAC_A, public_key=KEY, long_name="Delayed old name")
    with closing(open_current_database(path)) as connection:
        refresh_shared_numbers(connection)
    result = page(path)
    assert [item["node_id"] for item in result["items"]] == [NODE]
    reports = result["items"][0]["reports"]
    assert [(r["long_name"], r["mac_address"]) for r in reports] == [
        ("Renamed radio", "11:22:33:44:55:66"),
        ("Other radio", "aa:bb:cc:dd:ee:ff"),
    ]
    assert {r["public_key"] for r in reports} == {base64.b64encode(KEY).decode()}
    for report in reports:
        response = TestClient(create_app(Settings(path))).get(
            f"/api/v1/packets/{report['packet_row_id']}"
        )
        assert response.status_code == 200
    with closing(open_current_database(path)) as connection:
        recreate_derived(connection)
        assert rederive(connection, lambda: False)
    assert page(path) == result


def test_missing_zero_and_malformed_fields_do_not_create_conflicts(path: Path) -> None:
    send(path, 0, macaddr=MAC_A, public_key=KEY)
    send(path, 1, macaddr=MAC_A, long_name="New name")
    send(path, 2, public_key=KEY)
    send(path, 3, macaddr=b"\0" * 6, public_key=b"\0" * 32)
    send(path, 4, macaddr=b"bad", public_key=b"bad")
    assert page(path)["items"] == []


def test_key_changes_are_listed_even_when_the_mac_is_unchanged(path: Path) -> None:
    send(path, 0, macaddr=MAC_A, public_key=KEY)
    send(path, 1, macaddr=MAC_A, public_key=KEY[::-1])
    reports = page(path)["items"][0]["reports"]
    assert {r["public_key"] for r in reports} == {
        base64.b64encode(k).decode() for k in (KEY, KEY[::-1])
    }


def test_retention_removes_a_conflict_when_its_old_profile_expires(path: Path) -> None:
    send(path, 0, macaddr=MAC_A, public_key=KEY)
    send(path, 24 * 60, macaddr=MAC_B, public_key=KEY)
    assert len(page(path)["items"]) == 1
    with closing(open_current_database(path)) as connection:
        apply_retention(connection, retention_days=1, now=START + timedelta(days=1, minutes=1))
    assert page(path)["items"] == []


def test_hidden_senders_and_recipients_cannot_supply_conflict_evidence(path: Path) -> None:
    send(path, 0, macaddr=MAC_A)
    send(path, 1, macaddr=MAC_B)
    assert page(path)["items"]
    with closing(open_current_database(path)) as connection:
        connection.execute("INSERT INTO node_opt_out VALUES (?)", (NODE,))
    assert page(path)["items"] == []
    with closing(open_current_database(path)) as connection:
        connection.execute("DELETE FROM node_opt_out")
        connection.execute("INSERT INTO node_opt_out VALUES (42)")
        connection.execute("UPDATE packet SET recipient_node_id=42 WHERE packet_id=2")
    assert page(path)["items"] == []


def test_ids_are_paginated_and_profiles_are_bounded(path: Path) -> None:
    for node in [NODE, NODE + 1]:
        send(path, 0, node=node, macaddr=MAC_A, public_key=KEY)
        send(path, 1, node=node, macaddr=MAC_B, public_key=KEY)
    first = page(path, "?count=1")
    assert [item["node_id"] for item in first["items"]] == [NODE]
    assert first["next_node_id"] == NODE
    second = page(path, f"?count=1&after_node={NODE}")
    assert [item["node_id"] for item in second["items"]] == [NODE + 1]
    assert second["next_node_id"] is None
    for minute in range(2, 25):
        send(path, minute, macaddr=minute.to_bytes(6), public_key=KEY)
    item = page(path)["items"][0]
    assert len(item["reports"]) == 20
    assert item["limited"]


def test_conflicts_require_diagnostics_access(path: Path) -> None:
    client = TestClient(create_app(Settings(path, admin_password="private")))
    assert client.get(URL).status_code == 401
