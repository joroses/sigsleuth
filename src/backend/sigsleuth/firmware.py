"""Estimate the oldest firmware a node can be running from what its packets show.

Each Meshtastic release adds fields, roles and ports. A node whose packets use
one runs at least the release that added it, so the estimate is the newest such
trait seen: "2.8.0 or newer", never the version itself. Most nodes show nothing
newer than 2.6.8 while running a 2.7 release, and missing traits prove nothing
(licensed nodes send no public key on any version). The versions were checked
against the protobufs each firmware release ships. Fields newer than
the installed protobufs are found by their field number.
"""

import re
import zlib
from dataclasses import dataclass

from google.protobuf import unknown_fields  # type: ignore[attr-defined]
from google.protobuf.message import DecodeError, Message
from meshtastic.protobuf import (  # type: ignore[import-untyped]
    config_pb2,
    mesh_pb2,
    portnums_pb2,
    telemetry_pb2,
)


@dataclass(frozen=True)
class Trait:
    """version is major * 10000 + minor * 100 + patch, so 2.8.0 is 20800 and
    versions compare as numbers. description completes "Firmware 2.8.0 or
    newer: ..." on the node page."""

    version: int
    description: str


# Keys are stored in node_current.firmware_trait.
TRAITS = {
    "public_key": Trait(20500, "its NodeInfo carries a public key"),
    "relay_byte": Trait(20600, "its packet headers carry the relay byte"),
    "unmessagable_flag": Trait(20608, "its NodeInfo says whether it accepts messages"),
    "client_base_role": Trait(20709, "it uses the Client base role"),
    "node_status_port": Trait(20718, "it sends node status messages"),
    "noise_floor": Trait(20718, "its local statistics report a noise floor"),
    "traffic_telemetry": Trait(20720, "it sends traffic management telemetry"),
    "key_node_number": Trait(20800, "its node number is derived from its public key"),
    "signature": Trait(20800, "it signs its packets"),
    # Not in a release yet (2.8.0 is the newest); development builds send them.
    "ack_proof": Trait(20801, "its routing replies carry a delivery proof"),
    "paging_port": Trait(20801, "it sends pages"),
    "soil_water_telemetry": Trait(20801, "it sends soil and water telemetry"),
}


@dataclass(frozen=True)
class FirmwareEstimate:
    """The oldest firmware a node's packets prove it runs, such as "2.8.0", and
    why, such as "its node number is derived from its public key". The node may
    run any newer version."""

    minimum: str
    reason: str


def estimate(minimum: int | None, trait_key: str | None) -> FirmwareEstimate | None:
    """node_current's firmware columns as an estimate; None while no trait was seen."""
    trait = TRAITS.get(trait_key or "")
    if minimum is None or trait is None:
        return None
    return FirmwareEstimate(version_text(int(minimum)), trait.description)


# Field and port numbers the installed protobufs may not know yet.
_DATA_XEDDSA_SIGNATURE = 10
_ROUTING_ACK_PROOF = 4
_TELEMETRY_SOIL_WATER = 11
_PAGING_APP = 38


def version_text(version: int) -> str:
    """20800 -> "2.8.0"."""
    return f"{version // 10_000}.{version // 100 % 100}.{version % 100}"


def version_number(version: str | None) -> int | None:
    """A reported firmware string's leading major.minor.patch as a number:
    "2.7.15.567b8ea" -> 20715. None when it has none."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major * 10_000 + minor * 100 + patch


def newest(*traits: str | None) -> str | None:
    """The trait proving the newest firmware, None when there is none."""
    known = [trait for trait in traits if trait is not None]
    return max(known, key=lambda trait: TRAITS[trait].version, default=None)


def is_signed(data: mesh_pb2.Data) -> bool:
    """Whether the sender signed the packet's application data, which saved
    packets keep as is_signed: the "signature" trait."""
    return _has_field(data, _DATA_XEDDSA_SIGNATURE)


# The ports payload_trait reads.
TRAIT_PORTS = (
    portnums_pb2.PortNum.NODEINFO_APP,
    portnums_pb2.PortNum.TELEMETRY_APP,
    portnums_pb2.PortNum.ROUTING_APP,
    portnums_pb2.PortNum.NODE_STATUS_APP,
    _PAGING_APP,
)


def payload_trait(sender_node_id: int, portnum: int, payload: bytes) -> str | None:
    """The newest trait in a packet's application payload."""
    if portnum == portnums_pb2.PortNum.NODEINFO_APP:
        return _user_trait(sender_node_id, payload)
    if portnum == portnums_pb2.PortNum.TELEMETRY_APP:
        return _telemetry_trait(payload)
    if portnum == portnums_pb2.PortNum.ROUTING_APP:
        routing = _parse(mesh_pb2.Routing(), payload)
        return "ack_proof" if routing and _has_field(routing, _ROUTING_ACK_PROOF) else None
    if portnum == portnums_pb2.PortNum.NODE_STATUS_APP:
        return "node_status_port"
    if portnum == _PAGING_APP:
        return "paging_port"
    return None


def _user_trait(sender_node_id: int, payload: bytes) -> str | None:
    user = _parse(mesh_pb2.User(), payload)
    if user is None:
        return None
    if user.public_key and zlib.crc32(user.public_key) == sender_node_id:
        return "key_node_number"
    if user.role == config_pb2.Config.DeviceConfig.Role.CLIENT_BASE:
        return "client_base_role"
    if user.HasField("is_unmessagable"):
        return "unmessagable_flag"
    if user.public_key:
        return "public_key"
    return None


def _telemetry_trait(payload: bytes) -> str | None:
    telemetry = _parse(telemetry_pb2.Telemetry(), payload)
    if telemetry is None:
        return None
    if _has_field(telemetry, _TELEMETRY_SOIL_WATER):
        return "soil_water_telemetry"
    if telemetry.HasField("traffic_management_stats"):
        return "traffic_telemetry"
    if telemetry.HasField("local_stats") and telemetry.local_stats.noise_floor:
        return "noise_floor"
    return None


def _parse[M: Message](message: M, payload: bytes) -> M | None:
    try:
        message.ParseFromString(payload)
    except DecodeError:
        return None
    return message


def _has_field(message: Message, number: int) -> bool:
    """Whether the message carries the field, whether or not the installed
    protobufs know it."""
    return any(field.number == number for field, _ in message.ListFields()) or any(
        field.field_number == number for field in unknown_fields.UnknownFieldSet(message)
    )
