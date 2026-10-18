import json

import pytest
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, service_envelope
from sigsleuth.decode import (
    ApplicationInterpretation,
    describe_payload,
    interpret_payload,
    parse_delivery,
)


def application(topic: str, payload: bytes) -> ApplicationInterpretation:
    result = parse_delivery(topic, payload).application
    assert result is not None
    return result


def test_reads_header_facts_and_treats_unavailable_measurements_as_missing() -> None:
    topic = "msh/EU_868/2/e/LongFast/!aaaaaaaa"
    heard = parse_delivery(
        topic,
        application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"hello",
            encrypted=True,
            rx_snr=-7.25,
            rx_rssi=-110,
            hop_start=3,
            hop_limit=3,
            relay_node=0x4F,
        ),
    )
    uploaded_by_itself = parse_delivery(
        topic, application_envelope(portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello", hop_limit=3)
    )

    assert heard.gateway_node_id == 0xAAAAAAAA
    assert (heard.rx_snr, heard.rx_rssi, heard.hop_start, heard.hop_limit, heard.relay_byte) == (
        -7.25,
        -110,
        3,
        3,
        0x4F,
    )
    assert (
        uploaded_by_itself.rx_snr,
        uploaded_by_itself.rx_rssi,
        uploaded_by_itself.hop_start,
        uploaded_by_itself.hop_limit,
        uploaded_by_itself.relay_byte,
    ) == (None, None, None, None, None)


def test_a_zero_hop_start_is_known_only_on_a_packet_sent_with_no_hops() -> None:
    def hops(sender: int, hop_limit: int, relay_node: int) -> tuple[int | None, int | None]:
        parsed = parse_delivery(
            "msh/EU_868/2/e/LongFast/!aaaaaaaa",
            application_envelope(
                portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                b"hello",
                sender=sender,
                hop_start=0,
                hop_limit=hop_limit,
                relay_node=relay_node,
            ),
        )
        return parsed.hop_start, parsed.hop_limit

    # The sender's own relay byte proves firmware with hop counts.
    assert hops(0x0C0C0C42, 0, 0x42) == (0, 0)
    assert hops(0x0C0C0C00, 0, 0xFF) == (0, 0)
    # Otherwise 0 is older firmware.
    assert hops(0x0C0C0C42, 0, 0x11) == (None, None)
    assert hops(0x0C0C0C42, 0, 0) == (None, None)
    assert hops(0x0C0C0C42, 3, 0x42) == (None, None)


def test_keeps_gateway_and_header_facts_of_an_ungroupable_packet() -> None:
    result = parse_delivery(
        "msh/EU_868/2/map/",
        application_envelope(portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello", rx_snr=5.5),
    )

    assert result.unusable_reason == "invalid_map_report"
    assert result.application is None
    assert (result.gateway_node_id, result.rx_snr) == (0xAAAAAAAA, 5.5)


def test_treats_omitted_packet_channel_as_primary_channel() -> None:
    packet_payload = b"\x08\x01\x12\x05hello"
    result = parse_delivery(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        service_envelope(decoded_payload=packet_payload),
    )

    assert result.unusable_reason is None
    assert result.evidence is not None
    assert result.evidence.channel == "LongFast"
    assert result.evidence.sender_node_id == 0x11111111
    assert result.evidence.recipient_node_id == 0xFFFFFFFF
    assert result.evidence.packet_channel == 0
    assert result.evidence.packet_id == 0x12345678
    assert result.evidence.payload_kind == "decoded"
    assert result.evidence.portnum == portnums_pb2.PortNum.TEXT_MESSAGE_APP


def test_preserves_non_primary_packet_channel() -> None:
    result = parse_delivery(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        service_envelope(packet_channel=2),
    )

    assert result.evidence is not None
    assert result.evidence.packet_channel == 2


def test_accepts_custom_multi_segment_mqtt_root() -> None:
    result = parse_delivery(
        "site/msh/EU_868/2/e/LongFast/!aaaaaaaa",
        service_envelope(),
    )

    assert result.evidence is not None
    assert result.evidence.channel == "LongFast"


def test_resolves_channel_from_custom_root_topic_without_envelope_channel() -> None:
    packet_payload = b"\x08\x01\x12\x05hello"
    result = parse_delivery(
        "site/msh/EU_868/2/e/LongFast/!aaaaaaaa",
        service_envelope(decoded_payload=packet_payload, channel_id=""),
    )

    assert result.evidence is not None
    assert result.evidence.channel == "LongFast"


def test_leaves_custom_root_topic_with_wrong_suffix_marker_ungrouped() -> None:
    result = parse_delivery(
        "site/msh/EU_868/2/map/LongFast/!aaaaaaaa",
        service_envelope(),
    )

    assert result.evidence is None
    assert result.unusable_reason == "unrecognized_topic"


def test_accepts_encrypted_payload_as_opaque_grouping_evidence() -> None:
    packet_payload = b"sanitized ciphertext"
    result = parse_delivery(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        service_envelope(decoded_payload=None, encrypted_payload=packet_payload),
    )

    assert result.evidence is not None
    assert result.evidence.payload_kind == "encrypted"
    assert result.evidence.portnum is None


def test_reads_port_and_bytes_of_default_key_encrypted_payload() -> None:
    result = parse_delivery(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello", encrypted=True),
    )

    assert result.evidence is not None
    assert result.evidence.payload_kind == "encrypted"
    assert result.evidence.portnum == portnums_pb2.PortNum.TEXT_MESSAGE_APP
    assert result.application is not None
    assert result.application.payload == b"hello"


def test_reads_reply_and_reaction_of_a_text_message() -> None:
    topic = "msh/EU_868/2/e/LongFast/!aaaaaaaa"
    text = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    reply = application(topic, application_envelope(text, b"pong", reply_id=0xF358))
    reaction = application(
        topic, application_envelope(text, "👍".encode(), reply_id=0xF358, emoji=1, encrypted=True)
    )
    plain = application(topic, application_envelope(text, b"hello"))

    assert (reply.reply_id, reply.is_reaction) == (0xF358, False)
    assert (reaction.reply_id, reaction.is_reaction) == (0xF358, True)
    assert (plain.reply_id, plain.is_reaction) == (None, False)


def test_rejects_malformed_protobuf_with_safe_error() -> None:
    result = parse_delivery("msh/EU_868/2/e/LongFast/!aaaaaaaa", b"\x0a\xff")

    assert result.unusable_reason == "invalid_service_envelope"
    assert result.evidence is None


def test_leaves_missing_identity_ungrouped() -> None:
    result = parse_delivery(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        service_envelope(include_sender=False),
    )

    assert result.evidence is None
    assert result.unusable_reason == "missing_packet_identity"


def test_leaves_conflicting_topic_metadata_ungrouped() -> None:
    result = parse_delivery(
        "msh/US/2/e/LongFast/!bbbbbbbb",
        service_envelope(gateway_id="!aaaaaaaa"),
    )

    assert result.evidence is None
    assert result.unusable_reason == "unrecognized_topic"


def test_decodes_cleartext_node_info() -> None:
    user = mesh_pb2.User(
        id="!11111111",
        long_name="Hilltop Relay",
        short_name="HILL",
        hw_model=mesh_pb2.HardwareModel.RAK4631,
    )

    result = application(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString()),
    )

    assert result.node_info is not None
    assert result.node_info.long_name == "Hilltop Relay"
    assert result.node_info.short_name == "HILL"
    assert result.node_info.hardware_model == "RAK4631"
    assert result.node_info.role == "CLIENT"


def test_decodes_node_info_with_unknown_hardware_model() -> None:
    user = mesh_pb2.User(
        id="!11111111",
        long_name="Future Radio",
        short_name="FUTR",
        hw_model=9999,
        role=99,
    )

    result = application(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString()),
    )

    assert result.node_info is not None
    assert result.node_info.long_name == "Future Radio"
    assert result.node_info.hardware_model == "UNKNOWN_9999"
    assert result.node_info.role == "UNKNOWN_99"


def test_decrypts_default_key_position() -> None:
    position = mesh_pb2.Position(
        latitude_i=515_012_345,
        longitude_i=-1_234_567,
        altitude=127,
        timestamp=1_800_000_000,
    )

    result = application(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(
            portnums_pb2.PortNum.POSITION_APP,
            position.SerializeToString(),
            encrypted=True,
        ),
    )

    assert result.position is not None
    assert result.position.latitude == 51.5012345
    assert result.position.longitude == -0.1234567
    assert result.position.altitude_meters == 127
    assert result.position.device_time == 1_800_000_000_000
    assert result.position.is_valid is True


def test_treats_zero_altitude_as_missing() -> None:
    position = mesh_pb2.Position(latitude_i=515_012_345, longitude_i=-1_234_567, altitude=0)

    result = interpret_payload(portnums_pb2.PortNum.POSITION_APP, position.SerializeToString())

    assert result.position is not None
    assert result.position.altitude_meters is None


def test_marks_custom_key_application_unreadable() -> None:
    position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)

    result = application(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(
            portnums_pb2.PortNum.POSITION_APP,
            position.SerializeToString(),
            encrypted=True,
            key=bytes(range(16)),
        ),
    )

    assert result.unreadable_reason == "default_key_decryption_failed"
    assert result.position is None


def test_marks_an_empty_cleartext_payload_missing_except_for_a_traceroute() -> None:
    topic = "msh/EU_868/2/e/LongFast/!aaaaaaaa"

    position = application(topic, application_envelope(portnums_pb2.PortNum.POSITION_APP, b""))
    request = application(topic, application_envelope(portnums_pb2.PortNum.TRACEROUTE_APP, b""))

    assert position.unreadable_reason == "missing_application_payload"
    assert position.position is None
    assert request.unreadable_reason is None
    assert request.discovery is not None


def test_reads_an_encrypted_port_it_does_not_interpret() -> None:
    result = application(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"hello",
            encrypted=True,
        ),
    )

    assert (result.unreadable_reason, result.payload) == (None, b"hello")


@pytest.mark.parametrize("coordinate_i", [0, 8_192, 2_097_152])
def test_marks_placeholder_position_near_zero_invalid(coordinate_i: int) -> None:
    # Reduced position precision turns a 0,0 placeholder into a small offset.
    position = mesh_pb2.Position(latitude_i=coordinate_i, longitude_i=coordinate_i)

    result = application(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(portnums_pb2.PortNum.POSITION_APP, position.SerializeToString()),
    )

    assert result.position is not None
    assert result.position.is_valid is False
    assert result.position.invalid_reason == "zero_coordinates"


def test_gateway_that_is_not_a_node_id_is_unusable() -> None:
    result = parse_delivery(
        "msh/EU_868/2/e/LongFast/gateway-1",
        service_envelope(gateway_id="gateway-1"),
    )

    assert result.unusable_reason == "unrecognized_gateway"
    assert result.evidence is None


def test_reads_a_default_key_traceroute_reply_and_its_request_id() -> None:
    discovery = mesh_pb2.RouteDiscovery(route=[0x22222222], snr_towards=[20, -128])

    result = application(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            discovery.SerializeToString(),
            request_id=0x0BADF00D,
            encrypted=True,
        ),
    )

    assert result.request_id == 0x0BADF00D
    assert result.discovery is not None
    assert (result.discovery.route, result.discovery.snr_towards) == ((0x22222222,), (20, -128))
    assert result.discovery.route_back == ()


def test_reads_an_empty_traceroute_request_in_cleartext_and_default_key() -> None:
    for encrypted in (False, True):
        result = application(
            "msh/EU_868/2/e/LongFast/!aaaaaaaa",
            application_envelope(portnums_pb2.PortNum.TRACEROUTE_APP, b"", encrypted=encrypted),
        )

        assert result.discovery is not None
        assert result.discovery.route == ()


def test_marks_a_corrupted_traceroute_unreadable() -> None:
    result = application(
        "msh/EU_868/2/e/LongFast/!aaaaaaaa",
        application_envelope(portnums_pb2.PortNum.TRACEROUTE_APP, b"\x0a\x05ab"),
    )

    assert (result.unreadable_reason, result.discovery) == ("invalid_route_payload", None)


relays = list(range(0x10000001, 0x10000010))


@pytest.mark.parametrize(
    "discovery",
    [
        mesh_pb2.RouteDiscovery(route=relays[:7], snr_towards=[8] * 10),
        mesh_pb2.RouteDiscovery(route=relays[:9], snr_towards=[8] * 8),
        mesh_pb2.RouteDiscovery(route=relays[:8], snr_towards=[8] * 9),
        mesh_pb2.RouteDiscovery(route=relays[:2], snr_towards=[8, 5069]),
        mesh_pb2.RouteDiscovery(
            route=relays[:2], snr_towards=[8, 8, 8], route_back=relays[:1], snr_back=[8, 8, 8]
        ),
    ],
)
def test_marks_a_traceroute_firmware_cannot_send_unreadable(
    discovery: mesh_pb2.RouteDiscovery,
) -> None:
    result = interpret_payload(portnums_pb2.PortNum.TRACEROUTE_APP, discovery.SerializeToString())

    assert (result.unreadable_reason, result.discovery) == ("invalid_route_payload", None)


def test_reads_a_traceroute_whose_lists_are_full() -> None:
    discovery = mesh_pb2.RouteDiscovery(
        route=relays[:8], snr_towards=[-128] * 8, route_back=relays[:7], snr_back=[127] * 8
    )

    result = interpret_payload(portnums_pb2.PortNum.TRACEROUTE_APP, discovery.SerializeToString())

    assert result.discovery is not None
    assert len(result.discovery.route) == 8


def test_describes_payloads_as_text_json_or_hex() -> None:
    position = mesh_pb2.Position(latitude_i=520_000_000, longitude_i=40_000_000).SerializeToString()
    assert describe_payload(portnums_pb2.PortNum.TEXT_MESSAGE_APP, "héllo".encode()) == "héllo"
    assert json.loads(describe_payload(portnums_pb2.PortNum.POSITION_APP, position)) == {
        "latitude_i": 520000000,
        "longitude_i": 40000000,
    }
    assert describe_payload(portnums_pb2.PortNum.POSITION_APP, b"\xff") == "hex ff"
    assert describe_payload(999, bytes(100)).endswith("00 …")
