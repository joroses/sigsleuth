from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from sigsleuth.derive import HOUR_MS
from sigsleuth.packets import PacketReception
from sigsleuth.relay_checks import route_hint, route_relay, survey_stand_in
from sigsleuth.relays import NEAR_KM, DirectSignal, NearPartner, Relayed, RelayEvidence, relay_terms
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


def test_a_survey_receiver_hides_the_reporting_gateway(sample_client: TestClient) -> None:
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
        assert report["kind"] == "receiver" and not report["signal"]
        assert all(s["gateway_node_id"] != gateway for s in survey["signals"])
        assert all(link["node_id"] != gateway for link in survey["links"])
        assert all(
            c["near_partner"] is None or c["near_partner"]["node_id"] != gateway
            for c in survey["candidates"]
        )
    assert moved["candidates"] == default["candidates"]
    assert sample_client.get(url, params={"latitude": 52.0}).status_code == 400
    assert (
        sample_client.get(
            url, params={"receiver": "gateway", "latitude": 52.0, "longitude": 19.0}
        ).status_code
        == 400
    )


def test_survey_stand_in_drops_the_gateways_reach_reports_and_near_partners() -> None:
    at = 100 * HOUR_MS + 1_000
    hour = at - at % HOUR_MS
    partner = 0x11111111
    other_gw = 0x22222222
    near = NearPartner(52.0, 21.0, (hour - HOUR_MS,), (0, 5))
    evidence = RelayEvidence(
        by_byte={0xAA: [RELAY_A]},
        radio_links={GATEWAY: {RELAY_A: 10}, other_gw: {RELAY_A: 3}},
        sender_hops={REQUESTER: {RELAY_A: 4}},
        placed={},
        reach={RELAY_A: 65.0},
        radios={},
        active_hours={RELAY_A: [hour]},
        history_from=0,
        near={RELAY_A: {GATEWAY: near, partner: near}},
    )
    copy = Relayed(
        sender=REQUESTER,
        receiver=GATEWAY,
        at=at,
        position=(52.0, 21.0),
        radio=None,
        hop_start=3,
        hop_limit=2,
        relay_byte=0xAA,
        reports=[(GATEWAY, 2, 0xAA, 5.0), (other_gw, 2, 0xAA, 4.0)],
        rssi=-80.0,
    )
    survey_evidence, survey_copy = survey_stand_in(
        evidence,
        copy,
        partners={RELAY_A: {GATEWAY: 65.0, partner: 25.0}},
        sender_hops={REQUESTER: {RELAY_A: 1}},
        position=(52.01, 21.01),
    )
    assert survey_copy.receiver == 0
    assert survey_copy.position == (52.01, 21.01)
    assert survey_copy.reports == [(other_gw, 2, 0xAA, 4.0)]
    assert survey_evidence.reach == {RELAY_A: 25.0}
    assert survey_evidence.sender_hops == {REQUESTER: {RELAY_A: 1}}
    assert set(survey_evidence.near[RELAY_A]) == {partner}

    only_gateway_reach, _ = survey_stand_in(
        evidence,
        copy,
        partners={RELAY_A: {GATEWAY: 65.0}},
        sender_hops={},
    )
    assert only_gateway_reach.reach == {}
    terms = relay_terms(
        only_gateway_reach, survey_copy, {(RELAY_A, GATEWAY): DirectSignal(3, 5.0, 3, -82.0)}
    )
    assert "rheard" not in terms[RELAY_A]
    assert "r_rssi" not in terms[RELAY_A]


def test_a_moved_survey_receiver_reads_near_partners_around_its_place(
    sample_client: TestClient,
) -> None:
    checks = sample_client.get("/api/v1/diagnostics/relay-checks?count=500").json()["checks"]
    for check in checks:
        url = f"/api/v1/diagnostics/relay-checks/{check['reception_id']}"
        survey = sample_client.get(url).json()
        linked = [c for c in survey["candidates"] if c["near_partner"] is not None]
        if linked:
            break
    assert linked
    assert all(0 <= c["near_km"] <= NEAR_KM and c["near_observations"] > 0 for c in linked)
    at = survey["receiver"]
    # About 330 km north of the gateway, where no node of the sample stands.
    away = sample_client.get(
        url, params={"latitude": at["latitude"] + 3, "longitude": at["longitude"]}
    ).json()
    assert all(
        (c["near_partner"], c["near_km"], c["near_observations"]) == (None, None, 0)
        for c in away["candidates"]
    )
