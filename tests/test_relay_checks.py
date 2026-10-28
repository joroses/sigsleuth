from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from sigsleuth.packets import PacketReception
from sigsleuth.relay_checks import route_hint, route_relay
from sigsleuth.traceroute import ReportPaths, RoutePath

REQUESTER, RELAY_A, RELAY_B, GATEWAY, TARGET = (
    0x0A000001,
    0x0B0000AA,
    0x0C0000BB,
    0x0D00000D,
    0x0E0E,
)


def reception(
    forward: RoutePath, return_path: RoutePath | None = None, relay_byte: int = 0xBB
) -> PacketReception:
    return PacketReception(
        reception_id=1,
        received_at=datetime(2026, 10, 27, tzinfo=UTC),
        gateway_node_id=GATEWAY,
        gateway_name=None,
        gateway_short_name=None,
        gateway_latitude=None,
        gateway_longitude=None,
        gateway_estimated=False,
        rx_snr=None,
        rx_rssi=None,
        hop_start=3,
        hop_limit=1,
        relay_byte=relay_byte,
        paths=ReportPaths(forward, return_path),
    )


def test_a_route_names_the_node_before_its_gateway() -> None:
    request = RoutePath((REQUESTER, RELAY_A, RELAY_B, GATEWAY, TARGET), (4.0, -2.5, 6.75))
    assert route_relay(reception(request)) == (RELAY_B, 6.75)
    assert route_hint(reception(request)) is None
    # A reply's copy carried the return route; the gateway relayed the request.
    forward = RoutePath((REQUESTER, GATEWAY, RELAY_B, TARGET), (1.0, 2.0, 3.0))
    back = RoutePath((TARGET, RELAY_A, GATEWAY, REQUESTER), (5.0, -1.0))
    assert route_relay(reception(forward, back, relay_byte=0xAA)) == (RELAY_A, -1.0)
    assert route_hint(reception(forward, back, relay_byte=0xAA)) is None


@pytest.mark.parametrize(
    ("nodes", "hint"),
    [
        ((REQUESTER, RELAY_A, RELAY_B, TARGET), RELAY_B),
        ((REQUESTER, RELAY_A, TARGET), None),
        ((REQUESTER, RELAY_B, None, TARGET), None),
        ((REQUESTER, TARGET), None),
        ((REQUESTER, RELAY_B, GATEWAY, TARGET), None),
    ],
)
def test_only_a_matching_terminal_node_hints_at_a_non_appending_gateway(
    nodes: tuple[int | None, ...], hint: int | None
) -> None:
    path = RoutePath(nodes, ())
    assert route_hint(reception(path)) == hint
    assert route_hint(reception(RoutePath((REQUESTER, RELAY_A, TARGET), ()), path)) == hint


@pytest.mark.parametrize(
    ("path", "relay_byte"),
    [
        # Another node's byte: the route and the header disagree.
        (RoutePath((REQUESTER, RELAY_A, RELAY_B, GATEWAY, TARGET), (4.0, -2.5, 6.75)), 0xAA),
        # A gap before the gateway.
        (RoutePath((REQUESTER, RELAY_A, None, GATEWAY, TARGET), (4.0, None, 6.75)), 0xBB),
        # Exactly 0 dB likely crossed MQTT.
        (RoutePath((REQUESTER, RELAY_A, RELAY_B, GATEWAY, TARGET), (4.0, -2.5, 0.0)), 0xBB),
        # The gateway is not in it.
        (RoutePath((REQUESTER, RELAY_A, RELAY_B, TARGET), (4.0, -2.5)), 0xBB),
    ],
)
def test_a_route_names_no_relay_it_cannot_vouch_for(path: RoutePath, relay_byte: int) -> None:
    assert route_relay(reception(path, relay_byte=relay_byte)) is None


def test_relay_checks_agree_with_packet_pages(sample_client: TestClient) -> None:
    page = sample_client.get("/api/v1/diagnostics/relay-checks?count=500")
    assert page.status_code == 200
    checks = page.json()["checks"]
    assert checks
    outcomes = set()
    for check in checks[:40]:
        packet = sample_client.get(f"/api/v1/packets/{check['packet_row_id']}").json()
        shown = next(
            item for item in packet["receptions"] if item["reception_id"] == check["reception_id"]
        )
        named = check["page"]["relay"]
        relay = shown["relay_node"]
        assert (None if named is None else named["node_id"]) == (
            None if relay is None else relay["node_id"]
        )
        detail = sample_client.get(
            f"/api/v1/diagnostics/relay-checks/{check['reception_id']}"
        ).json()
        assert detail["check"]["survey"] == check["survey"]
        # A survey name is the likeliest candidate, sure enough and with evidence.
        possible = [c for c in detail["candidates"] if c["possible"]]
        likeliest = max(possible, key=lambda c: c["probability"], default=None)
        survey = check["survey"]["relay"]
        if survey is not None:
            assert likeliest is not None and survey["node_id"] == likeliest["node_id"]
            assert likeliest["probability"] >= detail["rules"]["name_probability"]
            assert likeliest["evidence"]
        total = sum(c["probability"] for c in possible) + detail["unknown_probability"]
        assert abs(total - 1) < 0.01
        outcomes.add(check["survey"]["outcome"])
    assert "right" in outcomes


def test_a_survey_receiver_keeps_the_gateway_as_evidence(sample_client: TestClient) -> None:
    checks = sample_client.get("/api/v1/diagnostics/relay-checks?count=500").json()["checks"]
    check = next(item for item in checks if item["page"]["how"] == "heard_directly")
    url = f"/api/v1/diagnostics/relay-checks/{check['reception_id']}"
    default = sample_client.get(url).json()
    gateway_mode = sample_client.get(url, params={"receiver": "gateway"}).json()
    at_gateway = gateway_mode["receiver"]
    assert default["receiver"]["node_id"] == 0
    assert default["receiver"]["latitude"] == at_gateway["latitude"]
    assert default["receiver"]["longitude"] == at_gateway["longitude"]
    assert any(candidate["heard_directly"] > 0 for candidate in gateway_mode["candidates"])
    moved = sample_client.get(
        url, params={"latitude": at_gateway["latitude"], "longitude": at_gateway["longitude"]}
    ).json()

    assert moved["moved"] and moved["receiver"]["placed_by"] == "chosen"
    gateway = check["gateway"]["node_id"]
    for survey in (default, moved):
        assert survey["check"]["page"] == check["page"]
        assert all(candidate["heard_directly"] == 0 for candidate in survey["candidates"])
        report = next(r for r in survey["reports"] if r["gateway"]["node_id"] == gateway)
        assert report["kind"] == "cohearer" and report["signal"]
    assert moved["candidates"] == default["candidates"]
    assert sample_client.get(url, params={"latitude": 52.0}).status_code == 400
    assert (
        sample_client.get(
            url, params={"receiver": "gateway", "latitude": 52.0, "longitude": 19.0}
        ).status_code
        == 400
    )
