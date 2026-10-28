from collections.abc import Mapping
from dataclasses import replace

from sigsleuth.db import NodeSummary
from sigsleuth.relays import (
    NAME_PROBABILITY,
    NodeRadio,
    Relayed,
    RelayEvidence,
    score_relays,
)

SENDER = 0x1000_0001
RECEIVER = 0x2000_0002
COHEARER = 0x3000_0003
AT = 1_800_000_000_000
HERE = (41.9, 1.5)
# About 1 km of latitude.
KM = 1 / 111.2


def node(
    node_id: int, km: float | None, *, role: str | None = "CLIENT", estimated: bool = False
) -> NodeSummary:
    return NodeSummary(
        node_id,
        None,
        None,
        role,
        None if km is None else HERE[0] + km * KM,
        None if km is None else HERE[1],
        estimated,
    )


def evidence(
    places: Mapping[int, NodeSummary | None],
    *,
    links: Mapping[int, Mapping[int, int]] | None = None,
    reach: Mapping[int, float] | None = None,
) -> RelayEvidence:
    nodes = list(places)
    return RelayEvidence(
        by_byte={0x3D: nodes},
        radio_links=links or {},
        sender_hops={},
        placed={n: p for n, p in places.items() if p is not None},
        reach=reach or {},
        radios={n: NodeRadio(None, None, None, AT) for n in nodes},
        active_hours={},
        history_from=None,
    )


def copy(*, cohearer: bool = False) -> Relayed:
    reports = [(COHEARER, 3, 0x3D, -5.0)] if cohearer else []
    return Relayed(SENDER, RECEIVER, AT, HERE, 11, 7, 3, 0x3D, reports)


def test_a_rival_lowers_the_best_ones_share_instead_of_vanishing() -> None:
    near = 0x0101_003D
    alone = score_relays(evidence({near: node(near, 2)}), copy(), {})
    for rival in (node(0x0202_003D, None), node(0x0202_003D, 300)):
        both = score_relays(evidence({near: node(near, 2), rival.node_id: rival}), copy(), {})
        assert rival.node_id in both.probabilities
        assert 0 < both.probabilities[rival.node_id]
        assert both.probabilities[near] < alone.probabilities[near]
        assert both.ranked()[0] == near


def test_a_candidate_with_no_evidence_is_never_named() -> None:
    # Unplaced, so nothing ties it to the copy; its role and channel alone
    # make it likely.
    router = 0x0101_003D
    found = evidence({router: node(router, None, role="ROUTER")})
    found = replace(found, radios={router: NodeRadio(11, None, None, AT)})
    scores = score_relays(found, copy(), {})
    assert scores.probabilities[router] >= NAME_PROBABILITY
    assert scores.named() is None


def test_evidence_names_a_candidate_and_words_the_name() -> None:
    linked = 0x0101_003D
    scores = score_relays(
        evidence({linked: node(linked, 30)}, links={COHEARER: {linked: 40}}),
        copy(cohearer=True),
        {},
    )
    assert scores.probabilities[linked] >= NAME_PROBABILITY
    assert scores.named() == (linked, "gateway_link")


def test_route_filtering_keeps_reports_from_an_earlier_hop() -> None:
    earlier, rival = 0x0101_003D, 0x0202_003D
    received = replace(
        copy(),
        reports=[(earlier, 4, 0x3D, 5.0), (COHEARER, 3, 0x3D, -5.0)],
        route_prefixes={RECEIVER: (earlier,), earlier: (), COHEARER: (rival,)},
    )
    found = evidence(
        {earlier: node(earlier, 2), rival: node(rival, 2)}, links={COHEARER: {rival: 100}}
    )
    assert score_relays(found, replace(received, route_prefixes={}), {}).named() == (
        rival,
        "gateway_link",
    )
    assert score_relays(found, received, {}).named() == (earlier, "reported")


def test_an_estimated_node_on_the_receiver_gains_nothing_from_its_distance() -> None:
    estimated, placed = 0x0101_003D, 0x0202_003D
    scores = score_relays(
        evidence(
            {
                estimated: node(estimated, 0.2, estimated=True),
                placed: node(placed, 0.2, estimated=False),
            }
        ),
        copy(),
        {},
    )
    assert set(scores.terms[estimated]) == {"estimated", "role_client"}
    assert "in_reach" in scores.terms[placed]
    assert not scores.evidenced(estimated)


def test_no_candidates_leave_only_an_unknown_node() -> None:
    scores = score_relays(evidence({}), copy(), {})
    assert scores.probabilities == {None: 1.0}
    assert scores.named() is None


def test_hints_keep_evidence_ahead_of_likelier_unsupported_candidates() -> None:
    nearby, router, client = 0x0101_003D, 0x0202_003D, 0x0303_003D
    found = evidence(
        {nearby: node(nearby, 19), router: node(router, None, role="ROUTER"), client: None}
    )
    found = replace(found, radios={router: NodeRadio(11, None, None, AT)})
    scores = score_relays(found, copy(), {})

    assert scores.ranked()[0] == router
    assert scores.hints() == [nearby, router, client]


def test_a_hidden_node_keeps_its_share_but_is_never_named() -> None:
    hidden, other = 0x0101_003D, 0x0202_003D
    places = {hidden: node(hidden, 1), other: node(other, 3)}
    shown = score_relays(evidence(places), copy(), {}, heard={hidden})
    assert shown.named() == (hidden, "receiver_heard")
    assert shown.named(hidden={hidden}) is None
    assert shown.probabilities[other] < 1 - NAME_PROBABILITY


def test_receiver_rssi_distinguishes_same_byte_candidates_and_missing_is_neutral() -> None:
    from sigsleuth.relays import DirectSignal

    a, b = 0x0101_003D, 0x0202_003D
    found = evidence({a: node(a, 2), b: node(b, 2)})
    profiles = {
        (a, RECEIVER): DirectSignal(0, None, 10, -80),
        (b, RECEIVER): DirectSignal(0, None, 10, -100),
    }
    received = replace(copy(), rssi=-80)
    scored = score_relays(found, received, profiles)
    assert scored.named() == (a, "receiver_heard")
    assert scored.terms[a]["r_rssi"] > 0
    assert -3 < scored.terms[b]["r_rssi"] < 0
    neutral = score_relays(found, copy(), profiles)
    assert neutral == score_relays(found, received, {})
    assert score_relays(found, replace(received, receiver=0), profiles) == neutral
    assert scored.named(hidden={a}) is None
    assert scored.probabilities[b] < 1 - NAME_PROBABILITY


def test_stronger_shortcut_falls_through_and_keeps_the_candidate() -> None:
    from sigsleuth.packets import name_copy
    from sigsleuth.relays import DirectSignal

    a = 0x0101_003D
    found = evidence({a: node(a, 1)})
    profile = {(a, RECEIVER): DirectSignal(0, None, 1, -100)}
    for rssi, skipped in ((None, False), (-120, False), (-85, False), (-84, True)):
        named = name_copy(found, replace(copy(), rssi=rssi), {(a, RECEIVER): 1}, profile)
        assert named.shortcut_skipped is skipped
        assert named.heard == {a: 1}
        assert named.scores.terms[a]["rheard"] == 1
        assert named.relay == ((a, "receiver_heard") if skipped else (a, "heard_directly"))
    missing = name_copy(found, replace(copy(), rssi=-50), {(a, RECEIVER): 1}, {})
    assert missing.relay == (a, "heard_directly")
