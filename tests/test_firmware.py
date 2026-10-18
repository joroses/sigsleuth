import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import (  # type: ignore[import-untyped]
    config_pb2,
    mesh_pb2,
    mqtt_pb2,
    portnums_pb2,
    telemetry_pb2,
)

from meshtastic_fixtures import application_envelope, ingest
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database
from sigsleuth.firmware import version_number

SENT_AT = datetime(2026, 9, 19, 9, 10, tzinfo=UTC)
KEY = bytes(range(32))
# A 2.8 node's number is the CRC32 of its public key.
KEYED_NODE = zlib.crc32(KEY)
OLD_NODE = 0x11111111
GATEWAY = 0xAAAAAAAA
NODEINFO = portnums_pb2.PortNum.NODEINFO_APP
TELEMETRY = portnums_pb2.PortNum.TELEMETRY_APP


@pytest.fixture
def path(tmp_path: Path) -> Path:
    database = tmp_path / "history.db"
    migrate_database(database)
    return database


def send(path: Path, packet_id: int, envelope: bytes) -> None:
    ingest(path, envelope, received_at=SENT_AT + timedelta(seconds=packet_id))


def node_info(sender: int, packet_id: int, **fields: Any) -> bytes:
    user = mesh_pb2.User(long_name="Hilltop", **fields)
    return application_envelope(
        NODEINFO, user.SerializeToString(), sender=sender, packet_id=packet_id
    )


def firmware(path: Path, node_id: int) -> dict[str, str] | None:
    with TestClient(create_app(Settings(path))) as client:
        node = client.get(f"/api/v1/nodes/{node_id}").json()["node"]
    return node["firmware"]  # type: ignore[no-any-return]


def test_node_number_from_the_public_key_proves_2_8(path: Path) -> None:
    send(path, 1, node_info(KEYED_NODE, 1, public_key=KEY))
    send(path, 2, node_info(OLD_NODE, 2, public_key=KEY))

    assert firmware(path, KEYED_NODE) == {
        "minimum": "2.8.0",
        "reason": "its node number is derived from its public key",
    }
    assert firmware(path, OLD_NODE) == {
        "minimum": "2.5.0",
        "reason": "its NodeInfo carries a public key",
    }


def test_the_minimum_only_rises(path: Path) -> None:
    send(path, 1, node_info(OLD_NODE, 1, role=config_pb2.Config.DeviceConfig.CLIENT_BASE))
    send(path, 2, node_info(OLD_NODE, 2, public_key=KEY))
    assert firmware(path, OLD_NODE) == {
        "minimum": "2.7.9",
        "reason": "it uses the Client base role",
    }

    # Soil and water telemetry (field 11) is newer than the installed protobufs.
    soil_water = telemetry_pb2.Telemetry(time=1).SerializeToString() + b"\x5a\x00"
    send(path, 3, application_envelope(TELEMETRY, soil_water, sender=OLD_NODE, packet_id=3))
    assert firmware(path, OLD_NODE) == {
        "minimum": "2.8.1",
        "reason": "it sends soil and water telemetry",
    }


def test_a_signed_packet_proves_2_8(path: Path) -> None:
    data = mesh_pb2.Data(portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, payload=b"hello")
    # Data field 10, xeddsa_signature, is newer than the installed protobufs.
    signed = data.SerializeToString() + b"\x52\x02\xab\xcd"
    packet = mesh_pb2.MeshPacket(to=0xFFFFFFFF, id=1)
    setattr(packet, "from", OLD_NODE)
    packet.decoded.ParseFromString(signed)
    envelope = mqtt_pb2.ServiceEnvelope(
        packet=packet, channel_id="LongFast", gateway_id="!aaaaaaaa"
    )
    send(path, 1, envelope.SerializeToString())

    assert firmware(path, OLD_NODE) == {"minimum": "2.8.0", "reason": "it signs its packets"}


def test_relay_byte_proves_2_6_for_the_gateway_and_a_sender_heard_directly(
    path: Path,
) -> None:
    relayed = 0x22222222
    send(
        path,
        1,
        application_envelope(
            TELEMETRY, b"", sender=relayed, packet_id=1, hop_start=3, hop_limit=2, relay_node=0x33
        ),
    )
    assert firmware(path, GATEWAY) == {
        "minimum": "2.6.0",
        "reason": "its packet headers carry the relay byte",
    }
    assert firmware(path, relayed) is None

    send(
        path,
        2,
        application_envelope(
            TELEMETRY,
            b"",
            sender=OLD_NODE,
            packet_id=2,
            hop_start=3,
            hop_limit=3,
            relay_node=OLD_NODE & 0xFF,
            rx_snr=5.0,
        ),
    )
    assert firmware(path, OLD_NODE) is not None


def test_nodes_page_sorts_by_firmware_either_way_with_unknown_last(path: Path) -> None:
    send(path, 1, node_info(OLD_NODE, 1, public_key=KEY))
    send(path, 2, node_info(KEYED_NODE, 2, public_key=KEY))
    send(path, 3, node_info(0x33333333, 3))

    def listed(sort: str) -> list[tuple[int, str | None]]:
        items = client.get("/api/v1/nodes", params={"sort": sort}).json()["items"]
        return [(item["node_id"], (item["firmware"] or {}).get("minimum")) for item in items]

    with TestClient(create_app(Settings(path))) as client:
        newest, oldest = listed("firmware"), listed("firmware_oldest")

    unknown = [(0x33333333, None), (GATEWAY, None)]
    assert newest == [(KEYED_NODE, "2.8.0"), (OLD_NODE, "2.5.0"), *unknown]
    assert oldest == [(OLD_NODE, "2.5.0"), (KEYED_NODE, "2.8.0"), *unknown]


@pytest.mark.parametrize(
    ("reported", "number"),
    (("2.7.17c.8f286bb16", 20717), ("2.6.4", 20604), ("2.10.1", 21001), ("", None), ("v2", None)),
)
def test_reads_the_version_number_from_a_reported_firmware_string(
    reported: str, number: int | None
) -> None:
    assert version_number(reported) == number
