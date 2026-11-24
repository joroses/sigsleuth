import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from derived_rows import link_hour
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database, now_ms, open_current_database
from sigsleuth.relays import DAY_MS
from test_coverage import (
    GATEWAY,
    LINKED_TO_GATEWAY,
    PHONE,
    RELAY,
    SENDER,
    SESSION,
    direct,
    measurement,
    mqtt_report,
    node,
    place_known_nodes,
    upload,
)

CHECKS = "/api/v1/diagnostics/survey-checks"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


@pytest.fixture
def client(path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(path, upload_password="survey"))
    with TestClient(app, headers={"Authorization": "Basic cGhvbmU6c3VydmV5"}) as client:
        yield client


@pytest.fixture
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(open_current_database(path)) as connection:
        yield connection


@pytest.fixture
def known_nodes(connection: sqlite3.Connection) -> None:
    place_known_nodes(connection)


def relayed(seq: int, **fields: Any) -> dict[str, Any]:
    """Packet 77 from SENDER, three hops used, relayed by a node sending 1A."""
    values = {"sender_node_id": SENDER, "packet_id": 77, "latitude": PHONE[0], **fields}
    values.setdefault("hop_start", 4)
    values.setdefault("hop_limit", 1)
    values.setdefault("relay_byte", 0x1A)
    return measurement(seq, **values)


def checks(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get(CHECKS, params=params)
    assert response.status_code == 200, response.text
    found: list[dict[str, Any]] = response.json()["checks"]
    return found


def node_id(item: dict[str, Any] | None) -> int | None:
    return None if item is None else item["node_id"]


@pytest.mark.usefixtures("known_nodes")
def test_each_measurement_shows_saved_and_todays_names(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    # Three candidates about as near: upload names none.
    upload(client, relayed(1))
    # Since then a gateway reported the same rebroadcast, and its earlier
    # direct readings of LINKED_TO_GATEWAY match it.
    mqtt_report(connection, GATEWAY, hop_limit=1, relay_byte=0x1A)
    link_hour(
        connection, LINKED_TO_GATEWAY, GATEWAY, now_ms() - 3_600_000, observations=3, snr=[5.0] * 3
    )
    [check] = checks(client)
    assert (check["upload_node"], check["upload_how"]) == (None, "unknown")
    assert node_id(check["today"]["node"]) == LINKED_TO_GATEWAY
    assert (check["today"]["how"], check["today"]["why"]) == ("gateway_link", "named")
    assert check["changed"]
    assert (check["shown_node"], check["shown_how"]) == (None, "unknown")

    # RELAY, unknown to MQTT, heard directly 5 km away is a rival: the
    # measurement is decided again, and now neither is sure enough.
    upload(client, direct(2, RELAY, latitude=PHONE[0] + 0.045))
    [check] = checks(client)
    assert (check["upload_node"], check["upload_how"]) == (None, "unknown")
    assert node_id(check["today"]["likeliest"]) == LINKED_TO_GATEWAY
    assert check["today"]["why"] == "below"
    assert not check["changed"]
    # The direct reception is listed only when asked for.
    assert [item["upload_how"] for item in checks(client, direct=True)] == ["direct", "unknown"]

    # The detail explains the same decisions.
    detail = client.get(f"{CHECKS}/{check['measurement_id']}").json()
    assert detail["check"] == check
    assert detail["receiver"]["placed_by"] == "phone"
    likeliest = max(
        (c for c in detail["candidates"] if c["possible"]), key=lambda c: c["probability"]
    )
    assert likeliest["node_id"] == LINKED_TO_GATEWAY
    assert [(r["gateway"]["node_id"], r["kind"], r["signal"]) for r in detail["reports"]] == [
        (GATEWAY, "cohearer", True)
    ]
    surveyed = next(c for c in detail["candidates"] if c["node_id"] == RELAY)
    assert {term["term"] for term in surveyed["terms"]} == {"survey_only", "sheard"}
    [linked] = detail["survey_link"]
    assert (linked["node_id"], linked["candidate"]) == (RELAY, True)
    assert linked["km"] == pytest.approx(5.0, abs=0.1)
    assert client.get(f"{CHECKS}/999").status_code == 404


def test_why_names_no_relay_byte_and_candidates_ruled_out_by_signal(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    # RELAY, the only node sending 1A, heard here at -100 dB: a copy at
    # -69 dB is too strong to be its.
    node(connection, RELAY, PHONE[0] + 0.01)
    upload(
        client,
        direct(1, RELAY, heard_at_ms=at - 60_000, rx_rssi=-92, rx_snr=-8.0),
        relayed(2, heard_at_ms=at, rx_rssi=-69, rx_snr=10.0),
        relayed(3, heard_at_ms=at - 1_000, relay_byte=None),
    )
    ruled_out, no_byte = checks(client)
    assert ruled_out["today"]["why"] == "ruled_out" and ruled_out["today"]["ruled_out"] == 1
    assert no_byte["today"]["why"] == "no_relay_byte"
    assert not ruled_out["changed"] and not no_byte["changed"]

    detail = client.get(f"{CHECKS}/{ruled_out['measurement_id']}").json()
    assert detail["ruled_out"] == [RELAY]
    assert detail["signal_db"] == -69
    [linked] = detail["survey_link"]
    assert (linked["node_id"], linked["too_strong"], linked["strongest_here_db"]) == (
        RELAY,
        True,
        -100,
    )
    assert not linked["candidate"]
    assert client.get(f"{CHECKS}/{no_byte['measurement_id']}").json()["candidates"] == []


def test_a_muted_node_heard_nearby_is_listed_but_never_scored_as_survey_heard(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    node(connection, RELAY, PHONE[0] + 0.01)
    connection.execute("UPDATE node_current SET role = 'CLIENT_MUTE' WHERE node_id = ?", (RELAY,))
    upload(client, direct(1, RELAY, heard_at_ms=at), relayed(2, heard_at_ms=at))
    [check] = checks(client)
    detail = client.get(f"{CHECKS}/{check['measurement_id']}").json()
    [linked] = detail["survey_link"]
    assert (linked["node_id"], linked["excluded"], linked["candidate"]) == (RELAY, "muted", False)
    [candidate] = detail["candidates"]
    assert "sheard" not in {term["term"] for term in candidate["terms"]}


def test_survey_checks_say_when_the_map_no_longer_counts_a_measurement(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    at = now_ms() - DAY_MS
    node(connection, RELAY, PHONE[0] + 0.01)
    upload(client, direct(1, RELAY, heard_at_ms=at), relayed(2, heard_at_ms=at))
    [check] = checks(client)
    assert (node_id(check["shown_node"]), check["shown_how"]) == (RELAY, "survey_link")
    assert check["counted"]
    # RELAY moved since: the map no longer counts what it was heard by.
    connection.execute("UPDATE node_current SET moved_at = ? WHERE node_id = ?", (now_ms(), RELAY))
    [check] = checks(client)
    assert (node_id(check["shown_node"]), check["shown_how"]) == (RELAY, "survey_link")
    assert not check["counted"]
    assert not client.get(f"{CHECKS}/{check['measurement_id']}").json()["check"]["counted"]


def test_survey_checks_list_newest_heard_first_and_page_on(client: TestClient) -> None:
    at = now_ms() - DAY_MS
    # Uploaded out of order, as a phone that was offline sends them.
    upload(client, relayed(1, heard_at_ms=at), relayed(2, heard_at_ms=at - 7_000))
    upload(
        client,
        relayed(1, heard_at_ms=at - 5_000),
        relayed(2, heard_at_ms=at),
        session={**SESSION, "id": "late"},
    )
    first = client.get(CHECKS, params={"count": 3}).json()
    rest = client.get(CHECKS, params={"count": 3, "before_id": first["next_before_id"]}).json()
    times = [check["heard_at"] for check in [*first["checks"], *rest["checks"]]]
    assert len(set(check["measurement_id"] for check in [*first["checks"], *rest["checks"]])) == 4
    assert times == sorted(times, reverse=True)
    assert rest["next_before_id"] is None


@pytest.mark.usefixtures("known_nodes")
def test_survey_checks_name_no_node_that_opted_out(
    client: TestClient, connection: sqlite3.Connection
) -> None:
    upload(client, relayed(1))
    mqtt_report(connection, GATEWAY, hop_limit=1, relay_byte=0x1A)
    link_hour(
        connection, LINKED_TO_GATEWAY, GATEWAY, now_ms() - 3_600_000, observations=3, snr=[5.0] * 3
    )
    connection.execute("INSERT INTO node_opt_out (node_id) VALUES (?)", (LINKED_TO_GATEWAY,))
    [check] = checks(client)
    assert check["today"]["node"] == {"node_id": None, "name": None}
    detail = client.get(f"{CHECKS}/{check['measurement_id']}")
    for response in (client.get(CHECKS), detail):
        assert str(LINKED_TO_GATEWAY) not in json.dumps(response.json())
