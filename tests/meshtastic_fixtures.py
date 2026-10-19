"""Sanitized Meshtastic envelope fixtures and a helper that saves them like capture."""

import struct
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import open_current_database, to_epoch_ms
from sigsleuth.ingest import IngestResult, MqttDelivery, ingest_delivery

DEFAULT_CHANNEL_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")


def service_envelope(
    *,
    sender: int = 0x11111111,
    recipient: int = 0xFFFFFFFF,
    packet_channel: int = 0,
    packet_id: int = 0x12345678,
    decoded_payload: bytes | None = b"\x08\x01\x12\x05hello",
    encrypted_payload: bytes | None = None,
    channel_id: str = "LongFast",
    gateway_id: str = "!aaaaaaaa",
    include_sender: bool = True,
) -> bytes:
    """Encode the official fields used by SigSleuth without private content."""
    packet = b""
    if include_sender:
        packet += _fixed32_field(1, sender)
    packet += _fixed32_field(2, recipient)
    if packet_channel != 0:
        packet += _varint_field(3, packet_channel)
    if decoded_payload is not None:
        packet += _bytes_field(4, decoded_payload)
    if encrypted_payload is not None:
        packet += _bytes_field(5, encrypted_payload)
    packet += _fixed32_field(6, packet_id)
    return b"".join(
        (
            _bytes_field(1, packet),
            _bytes_field(2, channel_id.encode()),
            _bytes_field(3, gateway_id.encode()),
        )
    )


def application_envelope(
    portnum: int,
    application_payload: bytes,
    *,
    sender: int = 0x11111111,
    recipient: int = 0xFFFFFFFF,
    packet_channel: int = 0,
    packet_id: int = 0x12345678,
    request_id: int = 0,
    reply_id: int = 0,
    emoji: int = 0,
    bitfield: int | None = None,
    encrypted: bool = False,
    key: bytes = DEFAULT_CHANNEL_KEY,
    channel_id: str = "LongFast",
    gateway_id: str = "!aaaaaaaa",
    **header: float | int,
) -> bytes:
    """Build a supported official application envelope, optionally AES-CTR encrypted.

    bitfield is Data.bitfield (bit 0 is OK to MQTT), left out when None.
    header sets MeshPacket header fields such as rx_snr, hop_start or relay_node.
    """
    data = mesh_pb2.Data(
        portnum=portnum,
        payload=application_payload,
        request_id=request_id,
        reply_id=reply_id,
        emoji=emoji,
        bitfield=bitfield,
    )
    packet = mesh_pb2.MeshPacket(
        to=recipient,
        channel=packet_channel,
        id=packet_id,
        **header,
    )
    setattr(packet, "from", sender)
    if encrypted:
        nonce = packet_id.to_bytes(8, "little") + sender.to_bytes(4, "little") + bytes(4)
        encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
        packet.encrypted = encryptor.update(data.SerializeToString()) + encryptor.finalize()
    else:
        packet.decoded.CopyFrom(data)
    return cast(
        bytes,
        mqtt_pb2.ServiceEnvelope(
            packet=packet,
            channel_id=channel_id,
            gateway_id=gateway_id,
        ).SerializeToString(),
    )


def map_report_envelope(
    node: int = 0x11111111, channel_id: str = "MediumFast", *, report: dict[str, object]
) -> bytes:
    """A map report as firmware publishes it on <root>/2/map/: a decoded
    MAP_REPORT_APP packet from the node itself, with packet ID 0."""
    data = mesh_pb2.Data(
        portnum=portnums_pb2.PortNum.MAP_REPORT_APP,
        payload=mqtt_pb2.MapReport(**report).SerializeToString(),
    )
    packet = mesh_pb2.MeshPacket(to=0xFFFFFFFF, decoded=data)
    setattr(packet, "from", node)
    return cast(
        bytes,
        mqtt_pb2.ServiceEnvelope(
            packet=packet, channel_id=channel_id, gateway_id=f"!{node:08x}"
        ).SerializeToString(),
    )


def ingest(
    path: Path,
    payload: bytes,
    *,
    received_at: datetime,
    topic: str = "msh/EU_868/2/e/LongFast/!aaaaaaaa",
) -> IngestResult:
    """Save one delivery through the same path capture uses."""
    with closing(open_current_database(path)) as connection:
        return ingest_delivery(connection, MqttDelivery(to_epoch_ms(received_at), topic, payload))


def _fixed32_field(field_number: int, value: int) -> bytes:
    return _varint((field_number << 3) | 5) + struct.pack("<I", value)


def _varint_field(field_number: int, value: int) -> bytes:
    return _varint(field_number << 3) + _varint(value)


def _bytes_field(field_number: int, value: bytes) -> bytes:
    return _varint(field_number << 3 | 2) + _varint(len(value)) + value


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)
