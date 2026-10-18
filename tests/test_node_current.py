from datetime import UTC, datetime, timedelta
from pathlib import Path

from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.db import migrate_database, open_database, to_epoch_ms
from sigsleuth.links import map_links
from sigsleuth.nodes import node_channels

TOPIC = "msh/EU_868/2/e/LongFast/!aaaaaaaa"


def test_one_packet_from_several_gateways_updates_the_node_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    user = mesh_pb2.User(long_name="Hilltop", short_name="HILL")
    first = ingest(
        path,
        application_envelope(portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString()),
        received_at=now,
    )
    second = ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.NODEINFO_APP,
            user.SerializeToString(),
            gateway_id="!bbbbbbbb",
        ),
        received_at=now + timedelta(seconds=2),
        topic="msh/EU_868/2/e/LongFast/!bbbbbbbb",
    )
    assert first.packet_row_id is not None
    assert second.packet_row_id == first.packet_row_id

    connection = open_database(path)
    try:
        packets = connection.execute("SELECT * FROM packet").fetchall()
        current = connection.execute(
            "SELECT * FROM node_current WHERE last_sent_at IS NOT NULL"
        ).fetchall()
        assert len(packets) == 1
        assert packets[0]["reception_count"] == 2
        assert packets[0]["unreadable_reason"] is None
        assert [(row["node_id"], row["last_sent_at"]) for row in current] == [
            (0x11111111, to_epoch_ms(now))
        ]
        assert current[0]["long_name"] == "Hilltop"
    finally:
        connection.close()


def test_later_readable_member_replaces_unreadable_application(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    opaque = application_envelope(
        portnums_pb2.PortNum.POSITION_APP,
        position.SerializeToString(),
        encrypted=True,
        key=bytes(range(16)),
    )
    readable = application_envelope(
        portnums_pb2.PortNum.POSITION_APP,
        position.SerializeToString(),
        gateway_id="!bbbbbbbb",
        packet_channel=7,
    )
    group_id = ingest(path, opaque, received_at=now).packet_row_id
    assert group_id is not None
    connection = open_database(path)
    try:
        initial = connection.execute("SELECT * FROM node_current").fetchone()
        assert initial is not None
        assert initial["last_sent_packet_row_id"] == group_id
        assert initial["position_packet_row_id"] is None
    finally:
        connection.close()
    second = ingest(
        path,
        readable,
        received_at=now + timedelta(seconds=1),
        topic="msh/EU_868/2/e/LongFast/!bbbbbbbb",
    )
    assert second.packet_row_id == group_id
    ingest(path, readable, received_at=now + timedelta(seconds=2))

    connection = open_database(path)
    try:
        row = connection.execute("SELECT unreadable_reason FROM packet").fetchone()
        assert row[0] is None
        current = connection.execute("SELECT * FROM node_current").fetchone()
        assert current is not None
        assert current["last_sent_packet_row_id"] == group_id
        assert current["position_packet_row_id"] == group_id
        assert current["latitude"] == 51.5
    finally:
        connection.close()


def test_current_node_values_use_newest_observation_of_each_kind(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)
    first_position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    tied_position = mesh_pb2.Position(latitude_i=520_000_000, longitude_i=20_000_000)
    old_position = mesh_pb2.Position(latitude_i=530_000_000, longitude_i=3_000_000)
    invalid_position = mesh_pb2.Position(latitude_i=0, longitude_i=0)

    def save(offset: int, packet_id: int, portnum: int, payload: bytes) -> int:
        packet_row_id = ingest(
            path,
            application_envelope(portnum, payload, packet_id=packet_id),
            received_at=now + timedelta(minutes=offset),
        ).packet_row_id
        assert packet_row_id is not None
        return packet_row_id

    first_info = save(
        2,
        1,
        portnums_pb2.PortNum.NODEINFO_APP,
        mesh_pb2.User(long_name="First", short_name="OLD").SerializeToString(),
    )
    save(1, 2, portnums_pb2.PortNum.POSITION_APP, first_position.SerializeToString())
    save(3, 3, portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"still active")
    save(
        0,
        4,
        portnums_pb2.PortNum.NODEINFO_APP,
        mesh_pb2.User(long_name="Late arrival").SerializeToString(),
    )
    latest_info = save(
        2,
        5,
        portnums_pb2.PortNum.NODEINFO_APP,
        mesh_pb2.User(long_name="Equal time").SerializeToString(),
    )
    save(4, 6, portnums_pb2.PortNum.POSITION_APP, invalid_position.SerializeToString())
    latest_position = save(
        1, 7, portnums_pb2.PortNum.POSITION_APP, tied_position.SerializeToString()
    )
    save(0, 8, portnums_pb2.PortNum.POSITION_APP, old_position.SerializeToString())
    latest_activity = save(4, 9, portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"newest")

    connection = open_database(path)
    try:
        current = connection.execute(
            "SELECT * FROM node_current WHERE last_sent_at IS NOT NULL"
        ).fetchall()
        assert len(current) == 1
        row = current[0]
        assert row["last_sent_packet_row_id"] == latest_activity
        assert row["last_sent_at"] == to_epoch_ms(now + timedelta(minutes=4))
        assert row["node_info_packet_row_id"] == latest_info
        assert row["node_info_observed_at"] == to_epoch_ms(now + timedelta(minutes=2))
        assert row["long_name"] == "Equal time"
        assert row["short_name"] is None
        assert row["position_packet_row_id"] == latest_position
        assert row["position_observed_at"] == to_epoch_ms(now + timedelta(minutes=1))
        assert (row["latitude"], row["longitude"]) == (52.0, 2.0)
        assert first_info < latest_info < latest_activity
        assert connection.execute("SELECT count(*) FROM packet").fetchone()[0] == 9
    finally:
        connection.close()


def test_only_radio_channels_set_the_node_channel(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    now = datetime(2026, 9, 19, 12, tzinfo=UTC)

    def send(channel: str, minutes: int) -> str | None:
        ingest(
            path,
            application_envelope(
                portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                b"hi",
                packet_id=0x1000 + minutes,
                channel_id=channel,
            ),
            received_at=now + timedelta(minutes=minutes),
            topic=f"msh/EU_868/2/e/{channel}/!aaaaaaaa",
        )
        connection = open_database(path)
        try:
            found: str | None = connection.execute(
                "SELECT channel FROM node_current WHERE node_id = ?", (0x11111111,)
            ).fetchone()[0]
            return found
        finally:
            connection.close()

    assert send("PKI", 0) is None
    assert send("longfast", 1) == "longfast"
    # Private messages and chat channels go out on the same radio.
    assert send("PKI", 2) == "longfast"
    assert send("TEST", 3) == "longfast"
    assert send("MediumFast", 4) == "MediumFast"


def test_node_channel_filter_keeps_links_with_the_observed_spelling(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"hi",
            channel_id="longfast",
            hop_start=3,
            hop_limit=3,
            relay_node=0x11,
            rx_snr=5,
            rx_rssi=-90,
        ),
        received_at=datetime(2026, 9, 19, 12, tzinfo=UTC),
        topic="msh/EU_868/2/e/longfast/!aaaaaaaa",
    )
    connection = open_database(path)
    try:
        channel = node_channels(connection).channels[0].channel
        links = map_links(
            connection,
            link_kind="direct",
            since=None,
            limit=100,
            default_preset="LongFast",
            channel=channel,
        ).items
        assert [(link.node_a, link.node_b, link.channel) for link in links] == [
            (0x11111111, 0xAAAAAAAA, "longfast")
        ]
    finally:
        connection.close()


SENDER, GATEWAY = 0x11111111, 0xAAAAAAAA


def position_at(
    path: Path, minutes: int, latitude: float, sender: int = SENDER, longitude: float = 2.0
) -> None:
    position = mesh_pb2.Position(latitude_i=int(latitude * 1e7), longitude_i=int(longitude * 1e7))
    ingest(
        path,
        application_envelope(
            portnums_pb2.PortNum.POSITION_APP,
            position.SerializeToString(),
            sender=sender,
            packet_id=sender + minutes,
        ),
        received_at=datetime(2026, 9, 19, 12, tzinfo=UTC) + timedelta(minutes=minutes),
    )


def moved_at(path: Path) -> int | None:
    connection = open_database(path)
    try:
        row = connection.execute(
            "SELECT moved_at FROM node_current WHERE node_id = ?", (SENDER,)
        ).fetchone()
        return None if row["moved_at"] is None else int(row["moved_at"])
    finally:
        connection.close()


def test_a_new_position_over_25_km_from_the_last_is_a_move(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    position_at(path, 0, 52.0)
    position_at(path, 1, 52.045)  # 5 km on
    assert moved_at(path) is None
    position_at(path, 2, 0.0, longitude=0.0)  # no fix: never replaces a valid position
    assert moved_at(path) is None

    position_at(path, 3, 52.5)  # 50 km on

    assert moved_at(path) == to_epoch_ms(datetime(2026, 9, 19, 12, 3, tzinfo=UTC))


def test_a_links_km_follows_the_latest_observation_of_its_hour(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    position_at(path, 0, 52.0, sender=GATEWAY)
    position_at(path, 0, 52.1)

    def heard_directly(minutes: int) -> None:
        ingest(
            path,
            application_envelope(
                portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                b"hi",
                packet_id=minutes,
                hop_start=3,
                hop_limit=3,
                relay_node=SENDER & 0xFF,
                rx_snr=5,
            ),
            received_at=datetime(2026, 9, 19, 12, tzinfo=UTC) + timedelta(minutes=minutes),
        )

    heard_directly(1)
    position_at(path, 2, 52.5)  # the sender drives 45 km away
    heard_directly(3)

    connection = open_database(path)
    try:
        [km] = connection.execute("SELECT km FROM link_hour WHERE link_kind = 'direct'").fetchone()
    finally:
        connection.close()
    assert km == 55.6
