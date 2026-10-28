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
