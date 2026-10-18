from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest, service_envelope
from sigsleuth.db import migrate_database, open_database, to_epoch_ms
from sigsleuth.ingest import IngestResult

RECEIVED_AT = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


def save(
    path: Path,
    *,
    gateway: str = "!aaaaaaaa",
    received_at: datetime = RECEIVED_AT,
    packet_id: int = 7,
    payload: bytes | None = None,
    topic: str | None = None,
) -> IngestResult:
    return ingest(
        path,
        payload or service_envelope(gateway_id=gateway, packet_id=packet_id),
        received_at=received_at,
        topic=topic or f"msh/EU_868/2/e/LongFast/{gateway}",
    )


def group(path: Path, **changes: Any) -> int | None:
    return save(path, **changes).packet_row_id


def count(path: Path, table: str) -> int:
    connection = open_database(path)
    try:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_groups_gateway_reports_and_preserves_receptions(path: Path) -> None:
    packet_row_id = group(path)
    assert (
        group(path, gateway="!bbbbbbbb", received_at=RECEIVED_AT + timedelta(seconds=2))
        == packet_row_id
    )
    assert count(path, "reception") == 2
    assert count(path, "packet") == 1


def test_sliding_inactivity_window_keeps_matching_chain_in_one_group(path: Path) -> None:
    groups = [
        group(path),
        group(path, received_at=RECEIVED_AT + timedelta(seconds=50)),
        group(path, received_at=RECEIVED_AT + timedelta(seconds=70)),
    ]
    assert groups[0] is not None and len(set(groups)) == 1
    assert count(path, "packet") == 1


def test_inactivity_window_joins_just_under_thirty_minutes_and_splits_at_boundary(
    path: Path,
) -> None:
    joined = [
        group(path, packet_id=7),
        group(path, packet_id=7, received_at=RECEIVED_AT + timedelta(minutes=30, milliseconds=-1)),
    ]
    split = [
        group(path, packet_id=8),
        group(path, packet_id=8, received_at=RECEIVED_AT + timedelta(minutes=30)),
    ]
    assert joined[0] is not None and joined[0] == joined[1]
    assert split[0] is not None and split[1] is not None and split[0] != split[1]


def test_out_of_order_match_does_not_move_latest_receipt_backward(path: Path) -> None:
    groups = [
        group(path),
        group(path, received_at=RECEIVED_AT + timedelta(seconds=50)),
        group(path, received_at=RECEIVED_AT + timedelta(seconds=40)),
    ]
    assert groups[0] is not None and len(set(groups)) == 1
    connection = open_database(path)
    try:
        latest = connection.execute("SELECT last_received_at FROM packet").fetchone()[0]
        assert latest == to_epoch_ms(RECEIVED_AT + timedelta(seconds=50))
    finally:
        connection.close()


def test_same_radio_packet_joins_across_topic_roots_channel_names_and_rewritten_payloads(
    path: Path,
) -> None:
    """Topic roots and channel names are gateway settings; relays rewrite some payloads."""
    groups = [
        group(path),
        group(path, topic="msh/EU_868/IT/2/e/LongFast/!aaaaaaaa"),
        group(
            path,
            payload=service_envelope(packet_id=7, channel_id="MediumFast"),
            topic="msh/PL/2/e/MediumFast/!aaaaaaaa",
        ),
        group(
            path, payload=service_envelope(packet_id=7, decoded_payload=b"\x08\x01\x12\x05other")
        ),
    ]
    assert groups[0] is not None and len(set(groups)) == 1
    assert count(path, "packet") == 1


def test_relay_rewritten_traceroute_stays_one_packet(path: Path) -> None:
    """Each relay appends itself to the route, so gateways upload different payloads."""

    def traceroute(route: list[int], hop_limit: int, gateway: str) -> bytes:
        return application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            mesh_pb2.RouteDiscovery(route=route).SerializeToString(),
            recipient=0x44444444,
            gateway_id=gateway,
            hop_start=3,
            hop_limit=hop_limit,
        )

    groups = [
        group(path, gateway=gateway, payload=traceroute(route, hop_limit, gateway))
        for route, hop_limit, gateway in (
            ([], 3, "!aaaaaaaa"),
            ([0x22222222], 2, "!bbbbbbbb"),
            ([0x22222222, 0x33333333], 1, "!cccccccc"),
        )
    ]
    assert groups[0] is not None and len(set(groups)) == 1
    assert count(path, "packet") == 1


def test_conflicting_radio_facts_start_new_packets(path: Path) -> None:
    text = b"\x08\x01\x12\x05hello"
    position = b"\x08\x03\x12\x02\x08\x01"
    groups = [
        group(path),
        group(path, payload=service_envelope(packet_id=7, sender=0x22222222)),
        group(path, payload=service_envelope(packet_id=7, recipient=0x33333333)),
        group(path, payload=service_envelope(packet_id=7, decoded_payload=position)),
        group(path, payload=service_envelope(packet_id=8, decoded_payload=text)),
    ]
    assert len(set(groups)) == 5


def test_missing_identity_is_skipped(path: Path) -> None:
    result = save(path, payload=service_envelope(packet_id=7, include_sender=False))
    assert result.reception_id is None
    assert result.unusable_reason == "missing_packet_identity"


def test_reception_matching_several_groups_joins_most_recently_heard(path: Path) -> None:
    text_group = group(path, payload=service_envelope(decoded_payload=b"\x08\x01\x12\x01a"))
    position_group = group(
        path,
        received_at=RECEIVED_AT + timedelta(seconds=5),
        payload=service_envelope(decoded_payload=b"\x08\x03\x12\x02\x08\x01"),
    )
    assert text_group != position_group
    unreadable = save(
        path,
        received_at=RECEIVED_AT + timedelta(seconds=10),
        payload=service_envelope(decoded_payload=None, encrypted_payload=b"custom key"),
    )
    assert unreadable.packet_row_id == position_group


@pytest.mark.parametrize(
    ("topic_channel", "envelope_channel", "expected"),
    [
        ("LongFast", "", "grouped"),
        ("", "LongFast", "grouped"),
        ("LongFast", "LongFast", "grouped"),
        ("LongFast", "MediumFast", "conflicting_channel_names"),
        ("", "", "missing_channel_name"),
    ],
)
def test_channel_name_resolution(
    path: Path, topic_channel: str, envelope_channel: str, expected: str
) -> None:
    first_group = group(path)
    second = save(
        path,
        topic=f"msh/EU_868/2/e/{topic_channel}/!bbbbbbbb",
        payload=service_envelope(gateway_id="!bbbbbbbb", channel_id=envelope_channel, packet_id=7),
    )
    if expected == "grouped":
        assert second.packet_row_id == first_group
    else:
        assert second.reception_id is None
        assert second.unusable_reason == expected


@pytest.mark.parametrize("first_kind", ["decoded", "encrypted"])
def test_mixed_forms_join_in_both_orders(path: Path, first_kind: str) -> None:
    decoded = service_envelope(decoded_payload=b"\x08\x01\x12\x05hello", packet_channel=4)
    encrypted = service_envelope(
        decoded_payload=None, encrypted_payload=b"opaque custom key", packet_channel=93
    )
    first_payload, second_payload = (
        (decoded, encrypted) if first_kind == "decoded" else (encrypted, decoded)
    )
    group_id = group(path, payload=first_payload)
    assert group(path, payload=second_payload) == group_id
    connection = open_database(path)
    try:
        row = connection.execute("SELECT * FROM packet WHERE id = ?", (group_id,)).fetchone()
        assert (row["has_decoded_report"], row["has_encrypted_report"]) == (1, 1)
        assert row["encrypted_channel_hash"] == 93
    finally:
        connection.close()


def test_encrypted_channel_hash_conflict_splits(path: Path) -> None:
    groups = [
        group(
            path,
            payload=service_envelope(
                decoded_payload=None, encrypted_payload=data, packet_channel=channel
            ),
        )
        for data, channel in ((b"same", 5), (b"different", 5), (b"same", 6))
    ]
    assert groups[0] == groups[1]
    assert len(set(groups)) == 2
