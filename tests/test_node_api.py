import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database

RECEIVED_AT = datetime(2026, 9, 19, 9, tzinfo=UTC)


def test_node_heard_through_several_topic_roots_is_one_map_node(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    _save_and_process(
        path,
        topic_root="msh/EU_868/2/e",
        received_at=RECEIVED_AT,
        packet_id=1,
        portnum=portnums_pb2.PortNum.POSITION_APP,
        application_payload=position.SerializeToString(),
    )
    _save_and_process(
        path,
        topic_root="msh/EU_868/IT/2/e",
        received_at=RECEIVED_AT + timedelta(minutes=1),
        packet_id=2,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"hello",
        channel_id="MediumFast",
    )

    with TestClient(create_app(Settings(path))) as client:
        items = client.get("/api/v1/map/nodes").json()["items"]
        node = client.get("/api/v1/nodes/286331153").json()["node"]

    assert [item["node_id"] for item in items] == [0x11111111]
    assert items[0]["last_sent_at"] == "2026-09-19T09:01:00Z"
    # The channel is the latest packet's.
    assert items[0]["channel"] == node["channel"] == "MediumFast"


def test_invalid_position_does_not_replace_latest_valid_map_position(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    valid = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    invalid = mesh_pb2.Position(latitude_i=0, longitude_i=0)
    for offset, packet_id, position in ((0, 1, valid), (1, 2, invalid)):
        _save_and_process(
            path,
            received_at=RECEIVED_AT + timedelta(minutes=offset),
            packet_id=packet_id,
            portnum=portnums_pb2.PortNum.POSITION_APP,
            application_payload=position.SerializeToString(),
        )

    with TestClient(create_app(Settings(path))) as client:
        items = client.get("/api/v1/map/nodes").json()["items"]

    assert items[0]["position"]["latitude"] == 51.5
    assert items[0]["position"]["observed_at"] == "2026-09-19T09:00:00Z"


def test_map_filters_activity_and_position_time_independently(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    _save_and_process(
        path,
        received_at=RECEIVED_AT,
        packet_id=1,
        portnum=portnums_pb2.PortNum.POSITION_APP,
        application_payload=position.SerializeToString(),
    )
    _save_and_process(
        path,
        received_at=RECEIVED_AT + timedelta(hours=2),
        packet_id=2,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"still active",
    )

    with TestClient(create_app(Settings(path))) as client:
        active_response = client.get(
            "/api/v1/map/nodes",
            params={
                "seen_since": "2026-09-19T10:00:00Z",
                "position_since": "2026-09-19T08:00:00Z",
            },
        )
        active = active_response.json()
        assert "server-timing" in active_response.headers
        assert "query;dur=" in active_response.headers["server-timing"]
        assert "serialization;dur=" in active_response.headers["server-timing"]
        position_too_old = client.get(
            "/api/v1/map/nodes",
            params={
                "seen_since": "2026-09-19T10:00:00Z",
                "position_since": "2026-09-19T10:00:00Z",
            },
        ).json()

    assert len(active["items"]) == 1
    assert active["items"][0]["last_sent_at"] == "2026-09-19T11:00:00Z"
    assert active["items"][0]["position"]["observed_at"] == "2026-09-19T09:00:00Z"
    # The sidebar links to both packets.
    assert active["items"][0]["last_sent_packet_row_id"] == 2
    assert active["items"][0]["position"]["packet_row_id"] == 1
    assert position_too_old["items"] == []


def test_map_time_filter_counts_a_gateway_report_as_seen(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    gateway = 0xAAAAAAAA  # the gateway _save_and_process reports through
    position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    _save_and_process(
        path,
        received_at=RECEIVED_AT,
        packet_id=1,
        portnum=portnums_pb2.PortNum.POSITION_APP,
        application_payload=position.SerializeToString(),
        sender=gateway,
    )
    _save_and_process(
        path,
        received_at=RECEIVED_AT + timedelta(hours=2),
        packet_id=2,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"heard by the gateway",
    )

    with TestClient(create_app(Settings(path))) as client:
        items = client.get(
            "/api/v1/map/nodes", params={"seen_since": "2026-09-19T10:00:00Z"}
        ).json()["items"]

    assert [item["node_id"] for item in items] == [gateway]
    assert items[0]["last_sent_at"] == "2026-09-19T09:00:00Z"
    assert items[0]["last_seen_at"] == "2026-09-19T11:00:00Z"
    assert items[0]["last_gateway_at"] == "2026-09-19T11:00:00Z"


def test_map_limits_to_most_recently_seen_positioned_nodes(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    for sender, packet_id, minutes in ((0x11111111, 1, 0), (0x22222222, 2, 1), (0x33333333, 3, 2)):
        _save_and_process(
            path,
            received_at=RECEIVED_AT + timedelta(minutes=minutes),
            packet_id=packet_id,
            portnum=portnums_pb2.PortNum.POSITION_APP,
            application_payload=position.SerializeToString(),
            sender=sender,
        )
    _save_and_process(
        path,
        received_at=RECEIVED_AT + timedelta(minutes=3),
        packet_id=4,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"no position",
        sender=0x44444444,
    )

    with TestClient(create_app(Settings(path))) as client:
        first = client.get("/api/v1/map/nodes", params={"limit": 2}).json()
        every = client.get("/api/v1/map/nodes").json()

    assert [item["node_id"] for item in first["items"]] == [0x33333333, 0x22222222]
    assert first["truncated"] is True
    assert len(every["items"]) == 3
    assert every["truncated"] is False


def test_map_rejects_invalid_parameters(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    with TestClient(create_app(Settings(path))) as client:
        naive_time = client.get("/api/v1/map/nodes", params={"seen_since": "2026-09-19T09:00"})
    assert naive_time.json()["error"]["code"] == "invalid_parameters"


def _save_and_process(
    path: Path,
    *,
    topic_root: str = "msh/EU_868/2/e",
    received_at: datetime,
    packet_id: int,
    portnum: int,
    application_payload: bytes,
    sender: int = 0x11111111,
    recipient: int = 0xFFFFFFFF,
    channel_id: str = "LongFast",
) -> int:
    packet_row_id = ingest(
        path,
        application_envelope(
            portnum,
            application_payload,
            sender=sender,
            recipient=recipient,
            packet_id=packet_id,
            channel_id=channel_id,
        ),
        received_at=received_at,
        topic=f"{topic_root}/{channel_id}/!aaaaaaaa",
    ).packet_row_id
    assert packet_row_id is not None
    return packet_row_id


def test_node_page_lists_packets_sent_by_and_addressed_to_the_node(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    node, other = 0x11111111, 0x22222222
    user = mesh_pb2.User(long_name="Hilltop Relay", short_name="HILL")
    _save_and_process(
        path,
        received_at=RECEIVED_AT,
        packet_id=1,
        portnum=portnums_pb2.PortNum.NODEINFO_APP,
        application_payload=user.SerializeToString(),
        sender=other,
    )
    sent = _save_and_process(
        path,
        received_at=RECEIVED_AT + timedelta(minutes=1),
        packet_id=2,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"to everyone",
    )
    received = _save_and_process(
        path,
        received_at=RECEIVED_AT + timedelta(minutes=2),
        packet_id=3,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"to you",
        sender=other,
        recipient=node,
    )
    # A broadcast by another node is not addressed to this one.
    _save_and_process(
        path,
        received_at=RECEIVED_AT + timedelta(minutes=3),
        packet_id=4,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"to everyone else",
        sender=other,
    )

    with TestClient(create_app(Settings(path))) as client:
        overview = client.get(f"/api/v1/nodes/{node}").json()
        page = client.get(f"/api/v1/nodes/{node}/packets").json()

    assert overview["node"]["node_id"] == node
    assert overview["node"]["last_sent_packet_row_id"] == sent
    assert overview["node"]["position"] is None
    assert [item["packet_row_id"] for item in page["packets"]] == [received, sent]
    assert page["packets"][0]["sender_name"] == "Hilltop Relay"
    assert page["packets"][0]["portnum"] == "TEXT_MESSAGE_APP"
    assert page["next_before_id"] is None


def test_node_page_filters_packets_by_port(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    node, other = 0x11111111, 0x22222222
    text = _save_and_process(
        path,
        received_at=RECEIVED_AT,
        packet_id=1,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"hello",
    )
    for packet_id in (2, 3):
        _save_and_process(
            path,
            received_at=RECEIVED_AT + timedelta(minutes=packet_id),
            packet_id=packet_id,
            portnum=portnums_pb2.PortNum.POSITION_APP,
            application_payload=mesh_pb2.Position(latitude_i=520000000).SerializeToString(),
        )
    direct = _save_and_process(
        path,
        received_at=RECEIVED_AT + timedelta(minutes=4),
        packet_id=4,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"to you",
        sender=other,
        recipient=node,
    )

    with TestClient(create_app(Settings(path))) as client:
        page = client.get(f"/api/v1/nodes/{node}/packets", params={"port": 1}).json()
        unknown = client.get(f"/api/v1/nodes/{node}/packets", params={"port": 999}).json()
        overview = client.get(f"/api/v1/nodes/{node}").json()

    assert [item["packet_row_id"] for item in page["packets"]] == [direct, text]
    assert unknown["packets"] == []
    # The ports it sent on, most packets first.
    assert [item["port"] for item in overview["ports"]] == ["POSITION_APP", "TEXT_MESSAGE_APP"]


def test_node_page_pages_through_packets_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    ids = [
        _save_and_process(
            path,
            received_at=RECEIVED_AT + timedelta(minutes=packet_id),
            packet_id=packet_id,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            application_payload=b"hello",
            sender=0x11111111 if packet_id % 2 else 0x22222222,
            recipient=0xFFFFFFFF if packet_id % 2 else 0x11111111,
        )
        for packet_id in range(1, 6)
    ]

    with TestClient(create_app(Settings(path))) as client:
        first = client.get("/api/v1/nodes/286331153/packets", params={"limit": 3}).json()
        second = client.get(
            "/api/v1/nodes/286331153/packets",
            params={"limit": 3, "before_id": first["next_before_id"]},
        ).json()

    listed = [item["packet_row_id"] for item in first["packets"] + second["packets"]]
    assert listed == ids[::-1]
    assert second["next_before_id"] is None


def test_node_page_shares_the_latest_node_info_as_a_contact_link(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    node, silent = 0x11111111, 0x22222222
    renamed = mesh_pb2.User(
        id="!11111111",
        long_name="Hilltop Relay",
        short_name="HILL",
        hw_model=mesh_pb2.HardwareModel.HELTEC_V3,
        public_key=bytes(range(32)),
        is_unmessagable=True,
    )
    for packet_id, user in enumerate((mesh_pb2.User(long_name="Old name"), renamed), start=1):
        _save_and_process(
            path,
            received_at=RECEIVED_AT + timedelta(minutes=packet_id),
            packet_id=packet_id,
            portnum=portnums_pb2.PortNum.NODEINFO_APP,
            application_payload=user.SerializeToString(),
        )
    _save_and_process(
        path,
        received_at=RECEIVED_AT,
        packet_id=3,
        portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        application_payload=b"hello",
        sender=silent,
    )

    with TestClient(create_app(Settings(path))) as client:
        url = client.get(f"/api/v1/nodes/{node}").json()["node"]["contact_url"]
        without = client.get(f"/api/v1/nodes/{silent}").json()["node"]["contact_url"]

    prefix, encoded = url.split("#")
    contact = admin_pb2.SharedContact.FromString(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    )
    assert prefix == "https://meshtastic.org/v/"
    assert contact == admin_pb2.SharedContact(node_num=node, user=renamed)
    assert without is None


def test_unknown_node_page_is_empty_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    with TestClient(create_app(Settings(path))) as client:
        unknown = client.get("/api/v1/nodes/42")
        packets = client.get("/api/v1/nodes/42/packets")
        invalid = client.get("/api/v1/nodes/0")

    assert unknown.json() == {"node": None, "ports": []}
    assert packets.json() == {"packets": [], "next_before_id": None}
    assert invalid.json()["error"]["code"] == "invalid_parameters"


def test_packet_list_pages_through_every_packet_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    user = mesh_pb2.User(long_name="Hilltop Relay", short_name="HILL")
    ids = [
        _save_and_process(
            path,
            received_at=RECEIVED_AT,
            packet_id=1,
            portnum=portnums_pb2.PortNum.NODEINFO_APP,
            application_payload=user.SerializeToString(),
        )
    ] + [
        _save_and_process(
            path,
            received_at=RECEIVED_AT + timedelta(minutes=packet_id),
            packet_id=packet_id,
            portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            application_payload=b"hello",
            sender=0x22222222,
            recipient=0x11111111 if packet_id == 2 else 0xFFFFFFFF,
        )
        for packet_id in range(2, 6)
    ]

    with TestClient(create_app(Settings(path))) as client:
        first = client.get("/api/v1/packets", params={"limit": 3}).json()
        second = client.get(
            "/api/v1/packets", params={"limit": 3, "before_id": first["next_before_id"]}
        ).json()

    listed = first["packets"] + second["packets"]
    assert [item["packet_row_id"] for item in listed] == ids[::-1]
    assert second["next_before_id"] is None
    assert listed[-2]["recipient_name"] == "Hilltop Relay"
    assert listed[-2]["sender_name"] is None


def test_node_contacts_show_every_distance_heard_and_relayed_gateways(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    node, near, relayed = 0x11111111, 0xAAAAAAAA, 0xBBBBBBBB
    home = mesh_pb2.Position(latitude_i=515_100_000, longitude_i=-1_000_000)
    # A GPS fix far away for one hour: the link heard then is measured from it.
    far = mesh_pb2.Position(latitude_i=560_000_000, longitude_i=210_000_000)

    def heard(
        sender: int, gateway: int, hour: int, position: mesh_pb2.Position, hops: int = 0
    ) -> None:
        """A position packet the gateway heard after `hops` relays (0: from the sender itself)."""
        ingest(
            path,
            application_envelope(
                portnums_pb2.PortNum.POSITION_APP,
                position.SerializeToString(),
                sender=sender,
                packet_id=0x100 + hour * 16 + gateway % 16 + sender % 7,
                gateway_id=f"!{gateway:08x}",
                hop_start=3,
                hop_limit=3 - hops,
                relay_node=sender & 0xFF if hops == 0 else 0x42,
                rx_snr=5,
            ),
            received_at=RECEIVED_AT + timedelta(hours=hour),
            topic=f"msh/EU_868/2/e/LongFast/!{gateway:08x}",
        )

    heard(near, near, 0, mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000))
    heard(relayed, relayed, 0, mesh_pb2.Position(latitude_i=516_000_000, longitude_i=-1_000_000))
    heard(node, near, 1, home)
    heard(node, near, 2, far)
    heard(node, near, 3, home)
    heard(node, relayed, 3, home, hops=2)

    with TestClient(create_app(Settings(path))) as client:
        page = client.get(
            f"/api/v1/nodes/{node}/contacts",
            params={"since": (RECEIVED_AT - timedelta(days=1)).isoformat()},
        ).json()

    first, second = page["items"]
    assert first["node_id"] == near and second["node_id"] == relayed
    (link,) = first["links"]
    assert link["kind"] == "direct" and link["observations"] == 3
    assert link["min_km"] < 2 and link["max_km"] > 1_000
    assert (first["heard_packets"], first["fewest_hops"]) == (3, 0)
    assert second["links"] == []
    assert (second["heard_packets"], second["fewest_hops"]) == (1, 2)
    assert page["packets"] == 4 and page["limited"] is False
