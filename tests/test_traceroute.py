from sigsleuth.decode import RouteDiscovery
from sigsleuth.traceroute import Hop, pinned_costs, report_hops, report_paths

A, B = 0xA0A0A0A0, 0xB0B0B0B0
R1, R2, R3 = 0x11111111, 0x22222222, 0x33333333
GATEWAY = 0x44444444
GAP = 0xFFFFFFFF


def route(
    route: tuple[int, ...] = (),
    snr_towards: tuple[int, ...] = (),
    route_back: tuple[int, ...] = (),
    snr_back: tuple[int, ...] = (),
) -> RouteDiscovery:
    return RouteDiscovery(route, snr_towards, route_back, snr_back)


def test_request_in_transit_has_hops_up_to_the_last_relay() -> None:
    paths = report_paths(route((R1, R2), (20, -6)), A, B, None)

    assert paths.return_path is None
    assert paths.forward.nodes == (A, R1, R2, B)
    assert paths.forward.snr_db == (5.0, -1.5)
    assert report_hops(paths) == [Hop(A, R1, 5.0), Hop(R1, R2, -1.5)]


def test_request_never_has_a_hop_into_its_target() -> None:
    # Seen from some gateways: an SNR added without the gateway adding itself.
    paths = report_paths(route((), (4,)), A, B, None)

    assert paths.forward.nodes == (A, B)
    assert report_hops(paths) == []


def test_reply_carries_the_finished_forward_path_and_its_own_return_path() -> None:
    # B answers A's request: the reply's sender is B and its recipient is A.
    reply = route((R1,), (8, 4), (R3,), (-12,))

    paths = report_paths(reply, B, A, request_id=0x1234)

    assert paths.forward.nodes == (A, R1, B)
    assert paths.return_path is not None
    assert paths.return_path.nodes == (B, R3, A)
    assert report_hops(paths) == [Hop(A, R1, 2.0), Hop(R1, B, 1.0), Hop(B, R3, -3.0)]


def test_one_hop_reply_is_read_by_its_request_id_not_its_empty_return_route() -> None:
    paths = report_paths(route((), (4,)), B, A, request_id=0x1234)

    assert paths.return_path is not None
    assert report_hops(paths) == [Hop(A, B, 1.0)]


def test_gap_is_kept_and_its_neighbours_are_never_joined() -> None:
    paths = report_paths(route((R1, GAP, R2), (4, -128, 8)), A, B, None)

    assert paths.forward.nodes == (A, R1, None, R2, B)
    assert report_hops(paths) == [Hop(A, R1, 1.0)]


def test_unknown_and_implausible_snr_keep_the_hop_without_a_value() -> None:
    paths = report_paths(route((R1, R2), (-128, 130)), A, B, None)

    assert report_hops(paths) == [Hop(A, R1, None), Hop(R1, R2, None)]


def test_step_at_exactly_0_db_crossed_mqtt_and_is_no_hop() -> None:
    # R1 got the request from MQTT downlink, so it added itself at 0 dB.
    paths = report_paths(route((R1, R2), (0, 8)), A, B, None)

    assert paths.forward.nodes == (A, R1, R2, B)
    assert report_hops(paths) == [Hop(R1, R2, 2.0)]


def test_hop_from_a_node_to_itself_is_skipped() -> None:
    paths = report_paths(route((R1, R1), (4, 8)), A, B, None)

    assert report_hops(paths) == [Hop(A, R1, 1.0)]


def costs(
    relays: tuple[int, ...], hops: int, snr: tuple[int, ...] | None = None, own_byte: bool = False
) -> list[tuple[int, int, bool]]:
    """pinned_costs of a request copy from A to B that GATEWAY heard after hops."""
    snr = tuple(20 for _ in relays) if snr is None else snr
    paths = report_paths(route(relays, snr), A, B, None)
    return [
        (hop.from_node_id, hop.to_node_id, free)
        for hop, free in pinned_costs(paths.forward, GATEWAY, hops, own_byte)
    ]


def test_one_hop_over_several_relays_makes_every_later_hop_free() -> None:
    # The first hop always costs, so it pins nothing.
    assert costs((R1, R2, R3), 1) == [(R1, R2, True), (R2, R3, True)]
    assert costs((R1,), 1) == []


def test_as_many_hops_as_relays_makes_every_later_hop_costly() -> None:
    assert costs((R1, R2, R3), 3) == [(R1, R2, False), (R2, R3, False)]


def test_other_hop_counts_pin_nothing() -> None:
    assert costs((R1, R2, R3), 2) == []
    # Fewer relays than hops: older firmware left some out.
    assert costs((R1, R2), 3) == []
    assert costs((R1, R2), 0) == []


def test_full_routes_and_gaps_pin_nothing() -> None:
    assert costs((R1, R2, R3, *range(1, 6)), 8) == []
    assert costs((R1, GAP, R2, R3), 4) == []


def test_a_gateway_uploading_its_own_rebroadcast_is_the_last_relay() -> None:
    assert costs((R1, R2, GATEWAY), 1, own_byte=True) == [(R1, R2, True), (R2, GATEWAY, True)]
    assert costs((R1, R2, GATEWAY), 3, own_byte=True) == [(R1, R2, False), (R2, GATEWAY, False)]
    # Its own byte without adding itself: the route and the header disagree.
    assert costs((R1, R2), 1, own_byte=True) == []


def test_a_gateway_that_added_itself_to_a_copy_it_heard_is_no_relay() -> None:
    assert costs((R1, R2, GATEWAY), 2) == [(R1, R2, False)]


def test_hops_that_are_no_hops_pin_nothing() -> None:
    # 0 dB likely crossed MQTT; a missing slot is a hop not measured.
    assert costs((R1, R2, R3), 1, (20, 0, 20)) == [(R2, R3, True)]
    assert costs((R1, R2, R3), 1, (20, 20)) == [(R1, R2, True)]
