from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meshtastic.protobuf import mesh_pb2, portnums_pb2  # type: ignore[import-untyped]

from meshtastic_fixtures import application_envelope, ingest, service_envelope
from sigsleuth.api import create_app
from sigsleuth.config import Settings
from sigsleuth.db import migrate_database

RECEIVED_AT = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)


def save(
    path: Path,
    *,
    received_at: datetime = RECEIVED_AT,
    gateway: str = "!aaaaaaaa",
    packet_id: int = 7,
    payload: bytes | None = None,
) -> tuple[int, int | None]:
    result = ingest(
        path,
        payload or service_envelope(gateway_id=gateway, packet_id=packet_id),
        received_at=received_at,
        topic=f"msh/EU_868/2/e/LongFast/{gateway}",
    )
    assert result.reception_id is not None
    return result.reception_id, result.packet_row_id


def test_detail_marks_direct_receptions_and_own_uploads(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    sender = 0x0C0C0C42

    def text(gateway: str, relay_node: int) -> tuple[int, int | None]:
        payload = application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"hello",
            sender=sender,
            gateway_id=gateway,
            hop_start=3,
            hop_limit=3,
            relay_node=relay_node,
            rx_snr=6.5,
        )
        return save(path, gateway=gateway, payload=payload)

    direct, packet_row_id = text("!aaaaaaaa", 0x42)
    relayed, _ = text("!bbbbbbbb", 0x11)
    own, _ = text("!0c0c0c42", 0x42)
    with TestClient(create_app(Settings(path))) as client:
        detail = client.get(f"/api/v1/packets/{packet_row_id}").json()
    assert detail["payload"] == "hello"
    assert {item["reception_id"]: item["direct"] for item in detail["receptions"]} == {
        direct: True,
        relayed: False,
        own: False,
    }
    # The sender uploading its own packet is no radio reception: it goes last.
    assert [(item["reception_id"], item["own_upload"]) for item in detail["receptions"]] == [
        (direct, False),
        (relayed, False),
        (own, True),
    ]


def test_a_sender_ending_in_00_is_heard_directly_by_its_ff_relay_byte(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    sender = 0x0C0C0C00
    # Firmware sends FF for a number ending in 00, as 0 means no relay.
    payload = application_envelope(
        portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        b"hello",
        sender=sender,
        hop_start=3,
        hop_limit=3,
        relay_node=0xFF,
        rx_snr=6.5,
    )
    reception_id, packet_row_id = save(path, payload=payload)
    with TestClient(create_app(Settings(path))) as client:
        detail = client.get(f"/api/v1/packets/{packet_row_id}").json()
        heard = client.get(f"/api/v1/packets?sender={sender}&heard_directly_by={0xAAAAAAAA}").json()
    assert [(item["reception_id"], item["direct"]) for item in detail["receptions"]] == [
        (reception_id, True)
    ]
    assert [item["packet_row_id"] for item in heard["packets"]] == [packet_row_id]


def test_detail_lists_receptions_nearest_the_sender_first(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    sender = 0x0C0C0C42

    # hop_start 0 is older firmware that does not report hops.
    def text(gateway: str, hop_limit: int, relay_node: int, hop_start: int = 3) -> int:
        payload = application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"hello",
            sender=sender,
            gateway_id=gateway,
            hop_start=hop_start,
            hop_limit=hop_limit,
            relay_node=relay_node,
            rx_snr=6.5,
        )
        return save(path, gateway=gateway, payload=payload)[0]

    two_hops = text("!aaaaaaaa", 1, 0x11)
    unknown = text("!bbbbbbbb", 0, 0x11, hop_start=0)
    no_hop_relayed = text("!cccccccc", 3, 0x11)
    one_hop = text("!dddddddd", 2, 0x11)
    direct = text("!eeeeeeee", 3, 0x42)
    with TestClient(create_app(Settings(path))) as client:
        detail = client.get("/api/v1/packets/1").json()
    assert [item["reception_id"] for item in detail["receptions"]] == [
        direct,
        no_hop_relayed,
        one_hop,
        two_hops,
        unknown,
    ]


def test_detail_shows_the_route_each_gateway_reported(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    requester, target, relay = 0x0A0A0A0A, 0x11111111, 0x22222222
    user = mesh_pb2.User(long_name="Hilltop Relay", short_name="HILL")
    save(
        path,
        payload=application_envelope(
            portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString(), sender=relay, packet_id=1
        ),
    )

    def reply(gateway: str, discovery: mesh_pb2.RouteDiscovery) -> tuple[int, int | None]:
        payload = application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            discovery.SerializeToString(),
            sender=target,
            recipient=requester,
            request_id=99,
            gateway_id=gateway,
            encrypted=True,
        )
        return save(path, gateway=gateway, payload=payload)

    # The target's own gateway uploads the reply before any relay touches it;
    # a gateway further along reports it after the relay added itself.
    at_target, packet_row_id = reply("!aaaaaaaa", mesh_pb2.RouteDiscovery(snr_towards=[24]))
    at_relay, _ = reply(
        "!bbbbbbbb",
        mesh_pb2.RouteDiscovery(snr_towards=[24], route_back=[relay], snr_back=[-128]),
    )
    # A return route that looks complete shows the reply arrived only when the
    # requester's own gateway reports it.
    complete = mesh_pb2.RouteDiscovery(snr_towards=[24], route_back=[relay], snr_back=[-128, 8])
    at_requester, _ = reply("!0a0a0a0a", complete)
    elsewhere, _ = reply("!cccccccc", complete)
    with TestClient(create_app(Settings(path))) as client:
        detail = client.get(f"/api/v1/packets/{packet_row_id}").json()

    paths = {item["reception_id"]: item["paths"] for item in detail["receptions"]}
    assert paths[at_target]["forward"] == {"nodes": [requester, target], "snr_db": [6.0]}
    assert paths[at_target]["return_path"] == {"nodes": [target, requester], "snr_db": []}
    assert paths[at_relay]["return_path"] == {"nodes": [target, relay, requester], "snr_db": [None]}
    # The relay's name reaches the route text even though it has no position.
    nodes = {node["node_id"]: node for node in detail["route_nodes"]}
    assert (nodes[relay]["long_name"], nodes[relay]["latitude"]) == ("Hilltop Relay", None)
    hops = {item["reception_id"]: item["hops"] for item in detail["receptions"]}
    assert hops[at_target] == [{"from_node_id": requester, "to_node_id": target, "snr_db": 6.0}]
    assert hops[at_relay] == [
        {"from_node_id": requester, "to_node_id": target, "snr_db": 6.0},
        {"from_node_id": target, "to_node_id": relay, "snr_db": None},
    ]
    reached = {item["reception_id"]: item["reached"] for item in detail["receptions"]}
    assert reached == {at_target: False, at_relay: False, at_requester: True, elsewhere: False}


def test_a_request_shows_the_route_its_reply_says_reached_the_target(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    requester, target, relay = 0x0A0A0A0A, 0x11111111, 0x22222222

    def traceroute(
        discovery: mesh_pb2.RouteDiscovery, packet_id: int, *, request_id: int = 0
    ) -> int | None:
        sender, recipient = (target, requester) if request_id else (requester, target)
        payload = application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            discovery.SerializeToString(),
            sender=sender,
            recipient=recipient,
            packet_id=packet_id,
            request_id=request_id,
        )
        return save(path, payload=payload)[1]

    request = traceroute(mesh_pb2.RouteDiscovery(route=[relay], snr_towards=[40]), 99)
    unanswered = traceroute(mesh_pb2.RouteDiscovery(), 100)
    reply = traceroute(
        mesh_pb2.RouteDiscovery(route=[relay], snr_towards=[40, 24]), 5, request_id=99
    )
    with TestClient(create_app(Settings(path))) as client:
        answered = client.get(f"/api/v1/packets/{request}").json()
        alone = client.get(f"/api/v1/packets/{unanswered}").json()
        of_reply = client.get(f"/api/v1/packets/{reply}").json()

    assert answered["reply_route"]["packet_row_id"] == reply
    assert answered["reply_route"]["path"] == {
        "nodes": [requester, relay, target],
        "snr_db": [10.0, 6.0],
    }
    assert answered["reply_route"]["hops"] == [
        {"from_node_id": requester, "to_node_id": relay, "snr_db": 10.0},
        {"from_node_id": relay, "to_node_id": target, "snr_db": 6.0},
    ]
    assert alone["reply_route"] is None
    assert of_reply["reply_route"] is None


def test_detail_lists_traceroute_hops_and_where_their_nodes_are(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    requester, target, relay = 0x0A0A0A0A, 0x11111111, 0x22222222
    position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    save(
        path,
        payload=application_envelope(
            portnums_pb2.PortNum.POSITION_APP,
            position.SerializeToString(),
            sender=relay,
            packet_id=1,
        ),
    )
    discovery = mesh_pb2.RouteDiscovery(snr_towards=[24], route_back=[relay], snr_back=[-20, 8])
    _, packet_row_id = save(
        path,
        received_at=RECEIVED_AT + timedelta(minutes=1),
        payload=application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            discovery.SerializeToString(),
            sender=target,
            recipient=requester,
            request_id=99,
            packet_id=2,
        ),
    )
    with TestClient(create_app(Settings(path))) as client:
        detail = client.get(f"/api/v1/packets/{packet_row_id}").json()

    hops = {(hop["from_node_id"], hop["to_node_id"]): hop["snr_db"] for hop in detail["hops"]}
    assert hops == {
        (requester, target): 6.0,
        (target, relay): -5.0,
        (relay, requester): 2.0,
    }
    # The relay has a position; the target sent the reply but none, and the
    # requester is known only from the reply's hops.
    assert [
        (node["node_id"], node["latitude"], node["longitude"]) for node in detail["route_nodes"]
    ] == [(requester, None, None), (target, None, None), (relay, 51.5, -0.1)]
    assert detail["spreading_factor"] == 11


def test_a_reply_is_traced_from_its_recipient_to_its_sender(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    requester, target = 0x0A0A0A0A, 0x11111111
    position = mesh_pb2.Position(latitude_i=515_000_000, longitude_i=-1_000_000)
    save(
        path,
        payload=application_envelope(
            portnums_pb2.PortNum.POSITION_APP,
            position.SerializeToString(),
            sender=requester,
            packet_id=1,
        ),
    )
    _, packet_row_id = save(
        path,
        payload=application_envelope(
            portnums_pb2.PortNum.TRACEROUTE_APP,
            b"",
            sender=target,
            recipient=requester,
            request_id=99,
            packet_id=2,
            encrypted=True,
        ),
    )
    with TestClient(create_app(Settings(path))) as client:
        detail = client.get(f"/api/v1/packets/{packet_row_id}").json()
    assert detail["traceroute"] == {"requester_node_id": requester, "target_node_id": target}
    assert [(node["node_id"], node["latitude"]) for node in detail["route_nodes"]] == [
        (requester, 51.5),
        (target, None),
    ]


def test_detail_says_why_the_payload_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    _, packet_row_id = save(
        path,
        payload=application_envelope(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            b"secret",
            encrypted=True,
            key=bytes(16),
        ),
    )
    with TestClient(create_app(Settings(path))) as client:
        detail = client.get(f"/api/v1/packets/{packet_row_id}").json()
    assert detail["payload"] is None
    assert detail["unreadable_reason"] == "default_key_decryption_failed"
    assert "custom key" in detail["unreadable_reason_description"]


def test_a_missing_packet_is_a_safe_404(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    with TestClient(create_app(Settings(path))) as client:
        missing = client.get("/api/v1/packets/999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "packet_not_found"


def test_the_admin_password_guards_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    with TestClient(create_app(Settings(path, admin_password="s3cret"))) as client:
        refused = client.get("/api/v1/diagnostics/summary")
        assert refused.status_code == 401
        assert refused.headers["WWW-Authenticate"].startswith("Basic")
        assert client.get("/api/v1/diagnostics/summary", auth=("x", "wrong")).status_code == 401
        assert client.get("/api/v1/diagnostics/summary", auth=("x", "s3cret")).status_code == 200
        assert client.get("/api/v1/nodes").status_code == 200

    with TestClient(create_app(Settings(path))) as client:
        assert client.get("/api/v1/diagnostics/summary").status_code == 200


def test_the_upload_password_guards_uploads_and_nothing_else(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)
    upload = {"session": {"id": "s", "receiver_node_id": 1}, "measurements": []}
    settings = Settings(path, admin_password="admin", upload_password="survey")
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/coverage/uploads", json=upload).status_code == 401
        refused = client.post("/api/v1/coverage/uploads", json=upload, auth=("", "admin"))
        assert refused.status_code == 401
        assert refused.json()["error"]["message"] == "This needs the upload password."
        accepted = client.post("/api/v1/coverage/uploads", json=upload, auth=("", "survey"))
        assert accepted.status_code == 200, accepted.text
        assert client.get("/api/v1/diagnostics/summary", auth=("x", "survey")).status_code == 401

    with TestClient(create_app(Settings(path, admin_password="admin"))) as client:
        response = client.post("/api/v1/coverage/uploads", json=upload, auth=("x", "admin"))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "uploads_disabled"


def test_unmigrated_database_is_unavailable_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(create_app(Settings(tmp_path / "missing.db"))) as client:
        response = client.get("/api/v1/packets/1")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    logged = [record for record in caplog.records if record.name == "sigsleuth.api"]
    assert logged and "/api/v1/packets/1" in logged[0].getMessage()
    assert logged[0].exc_info is not None


def test_api_serves_built_frontend_and_history_fallback(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>built app</h1>", encoding="utf-8")
    (frontend / "asset.txt").write_text("asset", encoding="utf-8")
    migrate_database(path)

    with TestClient(create_app(Settings(path), frontend)) as client:
        assert client.get("/").text == "<h1>built app</h1>"
        assert client.get("/nodes/123").text == "<h1>built app</h1>"
        assert client.get("/asset.txt").text == "asset"
        assert client.get("/api/v1/status").status_code == 200


def test_built_frontend_shows_the_configured_instance_name(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("<head><title>SigSleuth</title></head>", encoding="utf-8")
    migrate_database(path)

    with TestClient(create_app(Settings(path, name="Gaulix & co"), frontend)) as client:
        for page in ("/", "/index.html"):
            text = client.get(page).text
            assert "<title>Gaulix &amp; co · SigSleuth</title>" in text
            assert '<meta name="sigsleuth-name" content="Gaulix &amp; co">' in text


def test_missing_frontend_build_returns_instructions_without_hiding_api(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    migrate_database(path)

    with TestClient(create_app(Settings(path), tmp_path / "missing")) as client:
        page = client.get("/")
        status = client.get("/api/v1/status")

    assert page.status_code == 503
    assert "./scripts/setup.sh" in page.text
    assert status.status_code == 200
