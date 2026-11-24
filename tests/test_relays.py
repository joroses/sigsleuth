from collections.abc import Mapping, Sequence
from dataclasses import replace

from sigsleuth.db import NodeSummary
from sigsleuth.derive import HOUR_MS
from sigsleuth.radio import PRESET_RADIOS, Radio, modem_preset_radio
from sigsleuth.relays import (
    ACTIVE_MS,
    NAME_PROBABILITY,
    NEAR_KM,
    NEAR_MS,
    SURVEY_HEARD_SCORE,
    TERM_WEIGHTS,
    NearPartner,
    NodeRadio,
    Relayed,
    RelayEvidence,
    RelayTerm,
    cannot_relay,
    score_relays,
)

SENDER = 0x1000_0001
RECEIVER = 0x2000_0002
COHEARER = 0x3000_0003
AT = 1_800_000_000_000
HERE = (41.9, 1.5)
# About 1 km of latitude.
KM = 1 / 111.2
LONG_FAST = PRESET_RADIOS["LongFast"]


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
    channel: Radio | None = None,
) -> RelayEvidence:
    nodes = list(places)
    return RelayEvidence(
        by_byte={0x3D: nodes},
        radio_links=links or {},
        sender_hops={},
        placed={n: p for n, p in places.items() if p is not None},
        reach=reach or {},
        radios={n: NodeRadio(channel, None, None, AT) for n in nodes},
        active_hours={},
        history_from=None,
    )


def copy(*, cohearer: bool = False) -> Relayed:
    reports = [(COHEARER, 3, 0x3D, -5.0)] if cohearer else []
    return Relayed(SENDER, RECEIVER, AT, HERE, LONG_FAST, 7, 3, 0x3D, reports)


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
    found = replace(found, radios={router: NodeRadio(LONG_FAST, None, None, AT)})
    scores = score_relays(found, copy(), {})
    assert scores.probabilities[router] >= NAME_PROBABILITY
    assert scores.named() is None


def test_evidence_names_a_candidate_and_words_the_name() -> None:
    linked = 0x0101_003D
    scores = score_relays(
        evidence({linked: node(linked, 30)}, links={COHEARER: {linked: 40}}, channel=LONG_FAST),
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
    filtered = score_relays(found, received, {})
    assert "rep_plus1" in filtered.terms[earlier]
    assert "glink" not in filtered.terms[rival]
    assert filtered.ranked()[0] == earlier


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
    found = replace(found, radios={router: NodeRadio(LONG_FAST, None, None, AT)})
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
        assert named.relay is not None and named.relay[0] == a
        assert (named.relay[1] == "heard_directly") is not skipped
    missing = name_copy(found, replace(copy(), rssi=-50), {(a, RECEIVER): 1}, {})
    assert missing.relay == (a, "heard_directly")


def test_a_preset_on_another_bandwidth_cannot_relay() -> None:
    short_fast = NodeRadio(None, modem_preset_radio("SHORT_FAST"), None, AT)
    assert cannot_relay(short_fast, {PRESET_RADIOS["NarrowFast"]})
    assert not cannot_relay(short_fast, {PRESET_RADIOS["ShortFast"]})
    # A 2.4 GHz survey reports the widened bandwidth; an old one none.
    assert not cannot_relay(short_fast, {Radio(7, 812.5, exact=True)})
    assert not cannot_relay(short_fast, {Radio(7)})


def test_radio_channels_on_one_preset_are_separate_meshes() -> None:
    sf_narrow, narrow_fast = PRESET_RADIOS["SFNarrow"], PRESET_RADIOS["NarrowFast"]
    assert not sf_narrow.matches(narrow_fast)
    # A SFNarrow node's map report names the preset it runs.
    map_report = modem_preset_radio("NARROW_FAST")
    assert map_report is not None and map_report.matches(sf_narrow)
    # The survey app sends 869.618 for SFNarrow's 869.6187.
    sf_narrow_survey = Radio(7, 62.5, True, 869.618)
    assert sf_narrow_survey.matches(sf_narrow)
    assert not sf_narrow_survey.matches(narrow_fast)
    narrow_fast_survey = Radio(7, 62.5, True, 869.4312)
    assert narrow_fast_survey.matches(narrow_fast)
    assert not narrow_fast_survey.matches(sf_narrow)
    assert Radio(7, 62.5, exact=True).matches(narrow_fast)
    assert Radio(7, 62.5, exact=True).matches(sf_narrow)
    # A 2.4 GHz channel is never on a 250 kHz survey's settings.
    medium24 = PRESET_RADIOS["Medium24"]
    assert not medium24.matches(Radio(10, 250, exact=True))
    assert not medium24.matches(PRESET_RADIOS["MediumSlow"])
    assert medium24.matches(Radio(10, 812.5, exact=True))


def near(km: float, hours_before: Sequence[int], observations: int = 40) -> NearPartner:
    """A node km north of the receiver, with observations of links with a
    candidate in each of the hours this many before the copy's hour."""
    hour = AT - AT % HOUR_MS
    hours = sorted(hour - before * HOUR_MS for before in hours_before)
    totals = [observations * index for index in range(len(hours) + 1)]
    return NearPartner(HERE[0] + km * KM, HERE[1], tuple(hours), tuple(totals))


def test_links_with_a_node_near_the_receiver_name_an_unplaced_router() -> None:
    router, partner = 0x0101_003D, 0x4000_0004
    found = evidence({router: node(router, None, role="ROUTER")})
    found = replace(
        found,
        radios={router: NodeRadio(LONG_FAST, None, None, AT)},
        near={router: {partner: near(7, [2, 30])}},
    )
    assert score_relays(found, copy(), {}).named() == (router, "near_link")
    far = replace(found, near={router: {partner: near(NEAR_KM + 1, [2, 30])}})
    assert "nlink" not in score_relays(far, copy(), {}).terms[router]


def test_near_links_count_the_week_before_the_copys_hour() -> None:
    router, partner = 0x0101_003D, 0x4000_0004
    week = NEAR_MS // HOUR_MS
    for before, counted in ((0, False), (1, True), (week, True), (week + 1, False)):
        found = replace(
            evidence({router: node(router, None, role="ROUTER")}),
            near={router: {partner: near(7, [before])}},
        )
        assert ("nlink" in score_relays(found, copy(), {}).terms[router]) is counted, before


def test_the_receiver_is_not_its_own_near_partner() -> None:
    router = 0x0101_003D
    found = replace(
        evidence({router: node(router, None, role="ROUTER")}),
        near={router: {RECEIVER: near(0, [2])}},
    )
    assert "nlink" not in score_relays(found, copy(), {}).terms[router]


def nearby_score(terms: Mapping[RelayTerm, float]) -> float:
    """What near links and a survey's direct receptions add together."""
    nearby: tuple[RelayTerm, ...] = ("nlink", "nlink_log", "nlink_km", "sheard")
    return sum(TERM_WEIGHTS[term] * terms.get(term, 0.0) for term in nearby)


def test_a_survey_heard_candidate_is_named_by_the_survey() -> None:
    heard, other = 0x0101_003D, 0x0202_003D
    found = evidence({heard: node(heard, None), other: node(other, None)})
    assert score_relays(found, copy(), {}).named() is None
    scores = score_relays(found, copy(), {}, survey_heard={heard})
    assert scores.named() == (heard, "survey_link")


def test_strong_mqtt_evidence_beats_a_survey_heard_rival() -> None:
    router, client = 0x0101_003D, 0x0202_003D
    found = replace(
        evidence({router: node(router, 3, role="ROUTER"), client: node(client, 25)}),
        radios={
            router: NodeRadio(LONG_FAST, None, None, AT),
            client: NodeRadio(LONG_FAST, None, None, AT),
        },
        near={router: {0x4000_0004: near(2, [2, 30, 60])}},
    )
    scores = score_relays(found, copy(), {}, survey_heard={client})
    assert "sheard" in scores.terms[client]
    assert scores.named() == (router, "near_link")


def test_a_survey_tops_near_links_up_but_never_adds_to_them() -> None:
    strong, weak, alone = 0x0101_003D, 0x0202_003D, 0x0303_003D
    found = replace(
        evidence(
            {strong: node(strong, None), weak: node(weak, None), alone: node(alone, None)},
            channel=LONG_FAST,
        ),
        near={
            strong: {0x4000_0004: near(1, [2, 30, 60])},
            weak: {0x4000_0005: near(NEAR_KM - 1, [2], observations=1)},
        },
    )
    without = score_relays(found, copy(), {})
    scores = score_relays(found, copy(), {}, survey_heard={strong, weak, alone})
    assert scores.terms[strong] == without.terms[strong]
    assert nearby_score(without.terms[strong]) > SURVEY_HEARD_SCORE
    assert 0 < scores.terms[weak]["sheard"] < 1
    assert round(nearby_score(scores.terms[weak]), 6) == SURVEY_HEARD_SCORE
    assert scores.terms[alone]["sheard"] == 1


def test_mqtt_evidence_picks_between_two_survey_heard_nodes() -> None:
    gateway, tracker = 0x0101_003D, 0x0202_003D
    partner = 0x4000_0004
    found = replace(
        evidence(
            {gateway: node(gateway, 3), tracker: node(tracker, 3, estimated=True)},
            channel=LONG_FAST,
        ),
        near={
            gateway: {partner: near(3, list(range(2, 160)), observations=12)},
            tracker: {partner: near(3, [2, 30, 60], observations=20)},
        },
    )
    # The gateway uploaded the packet one hop earlier.
    received = replace(copy(), reports=[(gateway, 4, 0x2D, 6.0)])
    scores = score_relays(found, received, {}, survey_heard={gateway, tracker})
    assert scores.named() == (gateway, "near_link")


def test_a_survey_reception_stands_for_an_unknown_channel() -> None:
    heard, other = 0x0101_003D, 0x0202_003D
    found = evidence({heard: node(heard, 3), other: node(other, 3)})
    scores = score_relays(found, copy(), {}, survey_heard={heard})
    assert scores.terms[heard]["channel_same"] == 1
    assert "channel_same" not in scores.terms[other]
    on_another = evidence({heard: node(heard, 3)}, channel=PRESET_RADIOS["MediumFast"])
    terms = score_relays(on_another, copy(), {}, survey_heard={heard}).terms[heard]
    assert "channel_other" in terms
    assert "channel_same" not in terms


def test_a_survey_reception_within_a_day_shows_the_node_active() -> None:
    heard = 0x0101_003D
    found = replace(
        evidence({heard: node(heard, 3)}),
        radios={heard: NodeRadio(None, None, None, None)},
        history_from=AT - 10 * ACTIVE_MS,
    )
    assert "inactive" in score_relays(found, copy(), {}, survey_heard={heard}).terms[heard]
    active = score_relays(found, copy(), {}, survey_heard={heard}, survey_active={heard})
    assert "inactive" not in active.terms[heard]


def test_a_node_only_a_survey_heard_is_a_candidate() -> None:
    unknown, other, router = 0x0101_003D, 0x0202_003D, 0x0303_003D
    alone = score_relays(evidence({}), copy(), {}, survey_heard={unknown})
    assert alone.terms[unknown] == {"survey_only": 1.0, "sheard": 1.0}
    assert round(alone.probabilities[unknown], 3) == 0.953
    assert alone.named() == (unknown, "survey_link")

    tied = score_relays(evidence({}), copy(), {}, survey_heard={unknown, other})
    assert tied.probabilities[unknown] == tied.probabilities[other]
    assert tied.named() is None

    found = replace(
        evidence({router: node(router, 3, role="ROUTER")}, channel=LONG_FAST),
        near={router: {0x4000_0004: near(2, [2, 30, 60])}},
    )
    rival = score_relays(found, copy(), {}, survey_heard={unknown})
    assert "nlink_any" not in rival.terms[unknown]
    assert rival.named() == (router, "near_link")
