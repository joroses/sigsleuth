"""Read one MQTT delivery: the gateway, header facts, grouping facts and application data."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError, Message
from meshtastic.protobuf import (  # type: ignore[import-untyped]
    config_pb2,
    mesh_pb2,
    mqtt_pb2,
    paxcount_pb2,
    portnums_pb2,
    storeforward_pb2,
    telemetry_pb2,
)

from sigsleuth.firmware import is_signed
from sigsleuth.radio import relay_byte_of

HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
DEFAULT_CHANNEL_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
# Firmware's RouteDiscovery holds at most this many relays, and SNRs, per direction.
MAX_ROUTE_ENTRIES = 8

PayloadKind = Literal["decoded", "encrypted"]


@dataclass(frozen=True)
class PacketEvidence:
    """Radio facts used to decide whether two reports describe one packet.

    sender and packet ID identify the packet; recipient, application port
    and encrypted channel hash only rule out a match when they disagree.
    """

    channel: str
    sender_node_id: int
    recipient_node_id: int
    packet_channel: int
    packet_id: int
    payload_kind: PayloadKind
    portnum: int | None


@dataclass(frozen=True)
class NodeInfo:
    long_name: str | None
    short_name: str | None
    hardware_model: str | None
    role: str | None


@dataclass(frozen=True)
class Position:
    """device_time is the sender's clock, in milliseconds since the Unix epoch.
    location_source says where the sender got the position, such as LOC_MANUAL
    (typed in) or LOC_INTERNAL (its own GPS); None when unset."""

    latitude: float | None
    longitude: float | None
    altitude_meters: int | None
    device_time: int | None
    is_valid: bool
    invalid_reason: str | None
    location_source: str | None = None


@dataclass(frozen=True)
class MapReport:
    """What a node with its own MQTT connection says about itself on
    <root>/2/map/, by default once an hour (every 60 s on 2.5 and 2.6). It is
    not a radio event: no packet ID and no signal or hop facts.

    channel is the node's primary channel name (the envelope's channel_id).
    firmware_version is the full string, such as 2.7.15.567b8ea. region is None
    while unset. The position is rounded on purpose to position_precision bits
    (14 bits, the default, puts the true spot within about 1.5 km; 32 is
    exact); latitude and longitude are as sent, checked only when used.
    """

    node_id: int
    channel: str | None
    long_name: str | None
    short_name: str | None
    role: str
    hardware_model: str | None
    firmware_version: str | None
    region: str | None
    modem_preset: str
    has_default_channel: bool
    nodes_online_local: int
    latitude: float | None
    longitude: float | None
    altitude_meters: int | None
    position_precision: int


@dataclass(frozen=True)
class RouteDiscovery:
    """A traceroute's RouteDiscovery payload as one gateway reported it.

    Relays append their node number to route (or route_back on the return) and
    the SNR at which they heard the previous node to snr_towards (or snr_back),
    in quarter decibels. traceroute.report_paths turns this into paths and hops.
    """

    route: tuple[int, ...]
    snr_towards: tuple[int, ...]
    route_back: tuple[int, ...]
    snr_back: tuple[int, ...]


@dataclass(frozen=True)
class DeviceTelemetry:
    """What a node's own device telemetry says about it; None when unavailable.

    channel_utilization is the share of recent time the node heard the channel
    in use by anyone; air_util_tx the share of the last hour it transmitted,
    both in percent. battery_level is 1-100 %, or 101 for a node on external
    power.
    """

    channel_utilization: float | None
    air_util_tx: float | None
    battery_level: int | None


@dataclass(frozen=True)
class Neighbor:
    """A node another node's NeighborInfo says it heard directly, and the SNR
    in dB it heard it at; None when not measured."""

    node_id: int
    snr_db: float | None


@dataclass(frozen=True)
class ApplicationInterpretation:
    """What one report's application data says about its sender.

    unreadable_reason is the code saying why the application data could not be
    read (see diagnostics.py), None when it was read. Ports SigSleuth does not
    interpret, such as text messages, are readable. payload is the application
    bytes, None when there were none to read.
    request_id is the packet ID this one answers (set on traceroute replies and
    other responses), None when it answers nothing. reply_id is the packet ID of
    the text message this one replies to, None when it is not a reply; is_reaction
    marks a reply whose text is an emoji reaction (Data.emoji is set).
    is_signed marks data the sender signed (firmware 2.8.0 and newer).
    Telemetry is read later, from the saved payload (read_device_telemetry),
    so a fault in reading it cannot cost capture the delivery.
    """

    unreadable_reason: str | None = None
    payload: bytes | None = None
    request_id: int | None = None
    reply_id: int | None = None
    is_reaction: bool = False
    node_info: NodeInfo | None = None
    position: Position | None = None
    discovery: RouteDiscovery | None = None
    is_signed: bool = False


@dataclass(frozen=True)
class ParsedDelivery:
    """Everything capture reads from one MQTT delivery, parsed once.

    The signal and hop facts come from the packet header, which stays readable
    on encrypted packets. Unavailable measurements are None: an SNR or RSSI of 0
    (gateways upload their own and MQTT-relayed packets that way), a hop start of
    0 (older firmware; see known_hop_start), or a relay of 0. relay_byte is only
    the last byte of the relaying node's number.

    gateway_node_id, evidence and application are set when the delivery can be
    grouped, and map_report when it is a readable map report; otherwise
    unusable_reason says why it cannot be used, and capture skips it.
    """

    gateway_node_id: int | None = None
    rx_snr: float | None = None
    rx_rssi: int | None = None
    hop_start: int | None = None
    hop_limit: int | None = None
    relay_byte: int | None = None
    evidence: PacketEvidence | None = None
    application: ApplicationInterpretation | None = None
    map_report: MapReport | None = None
    unusable_reason: str | None = None


def known_hop_start(
    hop_start: int, hop_limit: int | None, relay_byte: int | None, sender_node_id: int
) -> int | None:
    """A header's hop start, None when unknown. 0 is firmware older than hop
    counts, except with no hops left on a packet whose relay byte is its
    sender's own: only firmware that fills in hop counts sends a relay byte, so
    the sender sent it with no hops."""
    if hop_start or (hop_limit == 0 and relay_byte == relay_byte_of(sender_node_id)):
        return hop_start
    return None


def parse_delivery(topic: str, payload: bytes) -> ParsedDelivery:
    """Read the gateway, header facts, grouping evidence and application data."""
    envelope = mqtt_pb2.ServiceEnvelope()
    try:
        # Trust protobuf's last-field-wins rule for a oneof rather than scan
        # the bytes a second time. Firmware publishes one payload form;
        # revisit only with a real ordinary-traffic sample containing both.
        envelope.ParseFromString(payload)
    except DecodeError:
        return ParsedDelivery(unusable_reason="invalid_service_envelope")
    if not envelope.HasField("packet"):
        return ParsedDelivery(unusable_reason="missing_mesh_packet")

    packet = envelope.packet
    relay_byte = int(packet.relay_node) or None
    hop_start = known_hop_start(
        int(packet.hop_start), int(packet.hop_limit), relay_byte, int(getattr(packet, "from"))
    )
    parsed = ParsedDelivery(
        gateway_node_id=node_number(envelope.gateway_id),
        rx_snr=round(float(packet.rx_snr), 2) or None,
        rx_rssi=int(packet.rx_rssi) or None,
        hop_start=hop_start,
        hop_limit=None if hop_start is None else int(packet.hop_limit),
        relay_byte=relay_byte,
    )
    if topic.rstrip("/").endswith("/2/map"):
        report = _read_map_report(envelope)
        if report is None:
            return replace(parsed, unusable_reason="invalid_map_report")
        return replace(parsed, map_report=report)
    unusable_reason = _unusable_reason(topic, envelope)
    if unusable_reason is not None:
        return replace(parsed, unusable_reason=unusable_reason)

    payload_kind: PayloadKind = (
        "decoded" if packet.WhichOneof("payload_variant") == "decoded" else "encrypted"
    )
    data, reason = _read_application_data(packet)
    return replace(
        parsed,
        evidence=PacketEvidence(
            channel=envelope.channel_id or topic.split("/")[-2],
            sender_node_id=_sender(packet),
            recipient_node_id=int(packet.to),
            packet_channel=int(packet.channel),
            packet_id=int(packet.id),
            payload_kind=payload_kind,
            portnum=int(data.portnum) if data is not None else None,
        ),
        application=_interpret_application(data, reason),
    )


def _interpret_application(data: mesh_pb2.Data | None, reason: str) -> ApplicationInterpretation:
    if data is None:
        return ApplicationInterpretation(unreadable_reason=reason)
    interpretation = interpret_payload(data.portnum, bytes(data.payload))
    return replace(
        interpretation,
        request_id=int(data.request_id) or None,
        reply_id=int(data.reply_id) or None,
        is_reaction=bool(data.emoji),
        is_signed=is_signed(data),
    )


def interpret_payload(portnum: int, payload: bytes) -> ApplicationInterpretation:
    """What an application payload says, from the delivery or as saved."""
    if portnum == portnums_pb2.PortNum.NODEINFO_APP:
        node_info = _read_node_info(payload)
        if node_info is None:
            return ApplicationInterpretation("invalid_node_info", payload)
        return ApplicationInterpretation(payload=payload, node_info=node_info)
    if portnum == portnums_pb2.PortNum.POSITION_APP:
        position = _read_position(payload)
        if position is None:
            return ApplicationInterpretation("invalid_position_payload", payload)
        return ApplicationInterpretation(payload=payload, position=position)
    if portnum == portnums_pb2.PortNum.TRACEROUTE_APP:
        discovery = read_route_discovery(payload)
        if discovery is None:
            return ApplicationInterpretation("invalid_route_payload", payload)
        return ApplicationInterpretation(payload=payload, discovery=discovery)
    return ApplicationInterpretation(payload=payload)


def read_route_discovery(payload: bytes) -> RouteDiscovery | None:
    """Read a traceroute's RouteDiscovery bytes; None if they are not one.

    An empty payload is a valid request that has not been relayed yet.

    Firmware keeps at most 8 relays and 8 SNRs in each direction, SNRs fit in
    a signed byte, and a direction has at most one SNR more than relays (the
    end's). Bit errors that pass the radio's CRC still decrypt and parse, so a
    payload breaking these rules is corrupted somewhere in it: it is
    unreadable as a whole, not just the out-of-place entries.
    """
    discovery = mesh_pb2.RouteDiscovery()
    try:
        discovery.ParseFromString(payload)
    except DecodeError:
        return None
    for route, snr in (
        (discovery.route, discovery.snr_towards),
        (discovery.route_back, discovery.snr_back),
    ):
        if (
            len(route) > MAX_ROUTE_ENTRIES
            or len(snr) > min(len(route) + 1, MAX_ROUTE_ENTRIES)
            or any(not -128 <= value <= 127 for value in snr)
        ):
            return None
    return RouteDiscovery(
        route=tuple(discovery.route),
        snr_towards=tuple(discovery.snr_towards),
        route_back=tuple(discovery.route_back),
        snr_back=tuple(discovery.snr_back),
    )


# Application ports whose payload is plain UTF-8 text.
_TEXT_PORTS = frozenset(
    {
        portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        portnums_pb2.PortNum.DETECTION_SENSOR_APP,
        portnums_pb2.PortNum.ALERT_APP,
        portnums_pb2.PortNum.RANGE_TEST_APP,
    }
)
# Application ports whose payload is one protobuf message.
_MESSAGE_PORTS: dict[int, Callable[[], Message]] = {
    portnums_pb2.PortNum.POSITION_APP: mesh_pb2.Position,
    portnums_pb2.PortNum.NODEINFO_APP: mesh_pb2.User,
    portnums_pb2.PortNum.ROUTING_APP: mesh_pb2.Routing,
    portnums_pb2.PortNum.WAYPOINT_APP: mesh_pb2.Waypoint,
    portnums_pb2.PortNum.PAXCOUNTER_APP: paxcount_pb2.Paxcount,
    portnums_pb2.PortNum.STORE_FORWARD_APP: storeforward_pb2.StoreAndForward,
    portnums_pb2.PortNum.TELEMETRY_APP: telemetry_pb2.Telemetry,
    portnums_pb2.PortNum.TRACEROUTE_APP: mesh_pb2.RouteDiscovery,
    portnums_pb2.PortNum.NEIGHBORINFO_APP: mesh_pb2.NeighborInfo,
    portnums_pb2.PortNum.MAP_REPORT_APP: mqtt_pb2.MapReport,
}
MAX_PAYLOAD_TEXT = 2_000
MAX_PAYLOAD_HEX_BYTES = 64


def describe_payload(portnum: int, payload: bytes) -> str:
    """A short readable form of application bytes for display.

    Text ports give their text, known message ports give compact JSON (bytes
    fields in base64), and anything else gives the leading bytes in hex. Long
    results are cut off with an ellipsis.
    """
    text: str | None = None
    if portnum in _TEXT_PORTS:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            pass
    elif portnum in _MESSAGE_PORTS:
        message = _MESSAGE_PORTS[portnum]()
        try:
            message.ParseFromString(payload)
            text = json.dumps(
                MessageToDict(message, preserving_proto_field_name=True), ensure_ascii=False
            )
        except DecodeError:
            pass
    if text is None:
        text = "hex " + payload[:MAX_PAYLOAD_HEX_BYTES].hex(" ")
        if len(payload) > MAX_PAYLOAD_HEX_BYTES:
            text += " …"
    if len(text) > MAX_PAYLOAD_TEXT:
        text = text[:MAX_PAYLOAD_TEXT] + "…"
    return text


def read_device_telemetry(payload: bytes) -> DeviceTelemetry | None:
    """Read ChUtil, AirUtil and battery level from a TELEMETRY_APP payload.

    None when it is not device telemetry or has none of them. Percentages
    outside 0-100 and battery levels of 0 (firmware sends 0 when it cannot
    measure) or above 101 are treated as missing.
    """
    telemetry = telemetry_pb2.Telemetry()
    try:
        telemetry.ParseFromString(payload)
    except DecodeError:
        return None
    if not telemetry.HasField("device_metrics"):
        return None
    metrics = telemetry.device_metrics

    def percent(field: str) -> float | None:
        if not metrics.HasField(field):
            return None
        value = float(getattr(metrics, field))
        return value if 0 <= value <= 100 else None

    level = int(metrics.battery_level) if metrics.HasField("battery_level") else None
    device = DeviceTelemetry(
        channel_utilization=percent("channel_utilization"),
        air_util_tx=percent("air_util_tx"),
        battery_level=level if level is not None and 1 <= level <= 101 else None,
    )
    if device == DeviceTelemetry(None, None, None):
        return None
    return device


def portnum_name(portnum: int) -> str:
    """Name an application port, e.g. 3 -> POSITION_APP."""
    return _enum_name(portnums_pb2.PortNum.Name, portnum)


def node_number(node_id: str) -> int | None:
    """Read a node ID such as !aabbccdd as its node number; None if it is not one."""
    digits = node_id[1:]
    if not node_id.startswith("!") or len(digits) != 8 or not HEX_DIGITS.issuperset(digits):
        return None
    return int(digits, 16)


def _unusable_reason(topic: str, envelope: mqtt_pb2.ServiceEnvelope) -> str | None:
    """Why a packet uploaded to <root>/2/e/<channel>/<gateway> cannot be grouped, if it can't.

    The topic root is a gateway setting, not a radio network, so it is checked
    for shape only and never used to identify nodes or packets.
    """
    topic_parts = topic.split("/")
    if (
        len(topic_parts) < 6
        or topic_parts[-4:-2] != ["2", "e"]
        or not all(topic_parts[:-2])
        or not envelope.gateway_id
        or topic_parts[-1] != envelope.gateway_id
    ):
        return "unrecognized_topic"
    if node_number(envelope.gateway_id) is None:
        return "unrecognized_gateway"
    topic_channel = topic_parts[-2]
    if topic_channel and envelope.channel_id and topic_channel != envelope.channel_id:
        return "conflicting_channel_names"
    if not topic_channel and not envelope.channel_id:
        return "missing_channel_name"

    packet = envelope.packet
    if _sender(packet) == 0 or packet.id == 0:
        return "missing_packet_identity"
    payload_variant = packet.WhichOneof("payload_variant")
    if not (
        (payload_variant == "decoded" and packet.decoded.ByteSize())
        or (payload_variant == "encrypted" and packet.encrypted)
    ):
        return "missing_packet_payload"
    return None


def _read_map_report(envelope: mqtt_pb2.ServiceEnvelope) -> MapReport | None:
    """Read a map report: a decoded MAP_REPORT_APP packet from the node itself,
    which the gateway ID names too. None when it is not one."""
    packet = envelope.packet
    node_id = _sender(packet) or node_number(envelope.gateway_id)
    if (
        not node_id
        or packet.WhichOneof("payload_variant") != "decoded"
        or packet.decoded.portnum != portnums_pb2.PortNum.MAP_REPORT_APP
        or not packet.decoded.payload
    ):
        return None
    report = mqtt_pb2.MapReport()
    try:
        report.ParseFromString(packet.decoded.payload)
    except DecodeError:
        return None
    hardware_model = _enum_name(mesh_pb2.HardwareModel.Name, report.hw_model)
    region = _enum_name(config_pb2.Config.LoRaConfig.RegionCode.Name, report.region)
    has_position = report.latitude_i != 0 or report.longitude_i != 0
    return MapReport(
        node_id=node_id,
        channel=_optional_text(envelope.channel_id),
        long_name=_optional_text(report.long_name),
        short_name=_optional_text(report.short_name),
        role=_enum_name(config_pb2.Config.DeviceConfig.Role.Name, report.role),
        hardware_model=None if hardware_model == "UNSET" else hardware_model,
        firmware_version=_optional_text(report.firmware_version),
        region=None if region == "UNSET" else region,
        modem_preset=_enum_name(config_pb2.Config.LoRaConfig.ModemPreset.Name, report.modem_preset),
        has_default_channel=report.has_default_channel,
        nodes_online_local=int(report.num_online_local_nodes),
        latitude=report.latitude_i / 10_000_000 if has_position else None,
        longitude=report.longitude_i / 10_000_000 if has_position else None,
        altitude_meters=int(report.altitude) or None,
        position_precision=int(report.position_precision),
    )


def _read_application_data(packet: mesh_pb2.MeshPacket) -> tuple[mesh_pb2.Data | None, str]:
    payload_variant = packet.WhichOneof("payload_variant")
    if payload_variant == "decoded":
        # As for decrypted bytes below, only a traceroute may be empty. A later
        # report of the packet that has its bytes replaces the unreadable result.
        data = packet.decoded
        if not data.payload and data.portnum != portnums_pb2.PortNum.TRACEROUTE_APP:
            return None, "missing_application_payload"
        return data, ""
    if payload_variant != "encrypted" or not packet.encrypted:
        return None, "missing_application_payload"

    data = _decrypt_application_data(packet)
    if data is None:
        return None, "default_key_decryption_failed"
    try:
        portnums_pb2.PortNum.Name(data.portnum)
    except ValueError:
        return None, "default_key_decryption_failed"
    if data.portnum == portnums_pb2.PortNum.UNKNOWN_APP:
        return None, "default_key_decryption_failed"
    # A traceroute request that has not been relayed yet carries an empty route.
    if not data.payload and data.portnum != portnums_pb2.PortNum.TRACEROUTE_APP:
        return None, "default_key_decryption_failed"
    return data, ""


def _decrypt_application_data(packet: mesh_pb2.MeshPacket) -> mesh_pb2.Data | None:
    nonce = int(packet.id).to_bytes(8, "little") + _sender(packet).to_bytes(4, "little") + bytes(4)
    decryptor = Cipher(algorithms.AES(DEFAULT_CHANNEL_KEY), modes.CTR(nonce)).decryptor()
    plaintext = decryptor.update(bytes(packet.encrypted)) + decryptor.finalize()
    data = mesh_pb2.Data()
    try:
        data.ParseFromString(plaintext)
    except DecodeError:
        return None
    return data


def _read_node_info(payload: bytes) -> NodeInfo | None:
    """Read a NodeInfo; role zero is CLIENT, while hardware zero is UNSET.

    Proto3 cannot distinguish an omitted role from explicit CLIENT, so both
    show the standard default role rather than an unknown one.
    """
    user = mesh_pb2.User()
    try:
        user.ParseFromString(payload)
    except DecodeError:
        return None
    if not payload:
        return None

    hardware_model = _enum_name(mesh_pb2.HardwareModel.Name, user.hw_model)
    role = _enum_name(config_pb2.Config.DeviceConfig.Role.Name, user.role)
    return NodeInfo(
        long_name=_optional_text(user.long_name),
        short_name=_optional_text(user.short_name),
        hardware_model=None if hardware_model == "UNSET" else hardware_model,
        role=role,
    )


def _enum_name(name_of: Callable[[int], str], value: int) -> str:
    """Fall back to a labeled number when the installed protobufs predate an enum value."""
    try:
        return name_of(value)
    except ValueError:
        return f"UNKNOWN_{value}"


def _read_position(payload: bytes) -> Position | None:
    position = mesh_pb2.Position()
    try:
        position.ParseFromString(payload)
    except DecodeError:
        return None
    if not payload:
        return None

    latitude = position.latitude_i / 10_000_000 if position.HasField("latitude_i") else None
    longitude = position.longitude_i / 10_000_000 if position.HasField("longitude_i") else None
    invalid_reason = invalid_position_reason(latitude, longitude)
    return Position(
        latitude=latitude,
        longitude=longitude,
        # 0 is a placeholder: nearly every zero altitude comes from a position
        # typed in by hand without one.
        altitude_meters=int(position.altitude) or None,
        device_time=_device_time(position),
        is_valid=invalid_reason is None,
        invalid_reason=invalid_reason,
        location_source=None
        if position.location_source == mesh_pb2.Position.LOC_UNSET
        else mesh_pb2.Position.LocSource.Name(position.location_source),
    )


def invalid_position_reason(latitude: float | None, longitude: float | None) -> str | None:
    if latitude is None or longitude is None:
        return "missing_coordinates"
    # Nodes without a fix report 0,0, and reduced position precision moves that to
    # a small offset such as 0.0008192,0.0008192 or 0.2097152,0.2097152. Everything
    # within a degree of 0,0 is open ocean, so treat it all as that placeholder.
    if abs(latitude) < 1 and abs(longitude) < 1:
        return "zero_coordinates"
    if not -90 <= latitude <= 90:
        return "latitude_out_of_range"
    if not -180 <= longitude <= 180:
        return "longitude_out_of_range"
    return None


def _device_time(position: mesh_pb2.Position) -> int | None:
    timestamp = int(position.timestamp or position.time)
    if timestamp == 0:
        return None
    return timestamp * 1_000


def _optional_text(value: str) -> str | None:
    stripped = value.strip()
    if not stripped or len(stripped.encode("utf-8")) > 1_024:
        return None
    return stripped


def _sender(packet: mesh_pb2.MeshPacket) -> int:
    return int(getattr(packet, "from"))


def read_neighbors(payload: bytes) -> tuple[Neighbor, ...]:
    """The neighbours a NEIGHBORINFO_APP payload lists; none if it is unreadable.

    Firmware lists at most 10 nodes it heard with no relay, and keeps one listed
    until it has been quiet for about two of its own broadcast intervals, so a
    list means "heard within the last few hours", not "heard now". When every
    SNR in a list is exactly 0 the firmware measured none, so they are
    missing. A 0 among other values is left out: firmware also lists a node
    it got from MQTT downlink at 0 dB. The sender itself and node numbers 0
    and broadcast are left out.
    """
    info = mesh_pb2.NeighborInfo()
    try:
        info.ParseFromString(payload)
    except DecodeError:
        return ()
    measured = any(neighbor.snr != 0 for neighbor in info.neighbors)
    return tuple(
        Neighbor(int(neighbor.node_id), float(neighbor.snr) if measured else None)
        for neighbor in info.neighbors
        if 0 < neighbor.node_id < 0xFFFFFFFF
        and neighbor.node_id != info.node_id
        and not (measured and neighbor.snr == 0)
    )
