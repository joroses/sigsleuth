"""Survey checks, beside relay checks in Diagnostics: uploaded survey
measurements and the node each one heard, decided three ways.

1. upload: heard_node at upload, saved once from what MQTT knew then;
2. survey link: coverage_relay, from other surveys' direct receptions;
3. today: heard_node's rules re-run now, with links around the
   measurement's hour.

The map shows the survey link, else upload's. Surveys carry no route, so
nothing here is right or wrong: the page compares the decisions.

Opted-out nodes read as "an opted-out node", as on relay checks.
"""

import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sigsleuth.coverage import (
    SURVEY_LINK_KM,
    SURVEY_LINK_TIERS_MS,
    TOO_STRONG_DB,
    TOO_STRONG_KM,
    HeardHow,
    MeasurementScore,
    SavedHow,
    heard_tier,
    is_direct,
    mqtt_reports,
    relay_evidence,
    score_measurement,
    shown_packet,
    signal_db,
    survey_candidates,
    survey_receptions,
    too_strong,
)
from sigsleuth.db import from_epoch_ms, nodes_by_id
from sigsleuth.derive import HOUR_MS, SavedHeader, is_direct_reception
from sigsleuth.opt_out import opted_out
from sigsleuth.positions import own_reach
from sigsleuth.radio import great_circle_km, relay_byte_of
from sigsleuth.relay_checks import (
    RelayCandidate,
    RelayCheckNode,
    RelayCheckReport,
    RelayLinks,
    RelayPlace,
    RelayRules,
    RelaySignal,
    explain_candidates,
    explain_evidence,
)
from sigsleuth.relays import (
    ACTIVE_MS,
    COHEARER_WINDOW_MS,
    DAY_MS,
    FAR_KM,
    NAME_KM,
    NAME_PROBABILITY,
    UNKNOWN_SCORE,
    Relayed,
    RelayEvidence,
    nodes_by_byte,
)
from sigsleuth.reported import positions_when_heard

# A measurement row (_ROWS).
Row = Mapping[str, Any]

SURVEY_CHECKS = 25
MAX_SURVEY_CHECKS = 500

# Why today's rules came out as they did: direct, or named; else no relay
# byte, every possible candidate too_strong, none possible, an unknown node
# likeliest, the likeliest below NAME_PROBABILITY, or with no evidence.
TodayWhy = Literal[
    "direct",
    "named",
    "no_relay_byte",
    "ruled_out",
    "no_candidates",
    "unknown",
    "below",
    "no_evidence",
]


@dataclass(frozen=True)
class SurveyToday:
    """What heard_node's rules name now, and the likeliest candidate when
    they name none. possible counts the candidates scored, ruled_out those
    too_strong left out."""

    node: RelayCheckNode | None
    how: SavedHow
    why: TodayWhy
    likeliest: RelayCheckNode | None
    probability: float | None
    possible: int
    ruled_out: int


@dataclass(frozen=True)
class SurveyCheck:
    """One measurement and its decisions. upload_node/upload_how are as
    saved; link is its survey link, link_nodes how many nodes survey
    evidence leaves it now (one makes a link). shown_node/shown_how are
    what the map shows; changed: today names another node than upload, a
    saved nearest guess reading as none."""

    measurement_id: int
    heard_at: datetime
    surveyor: RelayCheckNode
    sender: RelayCheckNode
    relay_byte: int | None
    hop_start: int | None
    hop_limit: int | None
    rx_snr: float | None
    rx_rssi: int | None
    upload_node: RelayCheckNode | None
    upload_how: SavedHow
    link: RelayCheckNode | None
    link_nodes: int
    today: SurveyToday
    shown_node: RelayCheckNode | None
    shown_how: HeardHow
    changed: bool


@dataclass(frozen=True)
class SurveyCheckList:
    """The newest measurements heard before before_id's, relayed ones only
    unless direct; next_before_id continues with older ones, None when none
    are left."""

    checks: tuple[SurveyCheck, ...]
    oldest_at: datetime | None
    newest_at: datetime | None
    next_before_id: int | None


_ROWS = """
    SELECT m.id, m.heard_at, m.latitude, m.longitude, m.sender_node_id, m.packet_id,
           m.rx_snr, m.rx_rssi, m.hop_start, m.hop_limit, m.relay_byte,
           m.heard_node_id, m.heard_how,
           s.receiver_node_id, s.receiver_name, s.spreading_factor, s.bandwidth_khz,
           s.frequency_mhz, l.node_id AS linked
    FROM coverage_measurement AS m {index}
    JOIN coverage_session AS s ON s.id = m.session_row_id
    LEFT JOIN coverage_relay AS l ON l.measurement_id = m.id
"""


def survey_checks(
    connection: sqlite3.Connection, *, before_id: int | None, count: int, direct: bool = False
) -> SurveyCheckList:
    """The checks of the `count` newest heard measurements before measurement
    before_id (the newest when None)."""
    before = None
    if before_id is not None:
        before = connection.execute(
            "SELECT heard_at FROM coverage_measurement WHERE id = ?", (before_id,)
        ).fetchone()
        if before is None:
            return SurveyCheckList((), None, None, None)
    rows = [
        dict(row)
        for row in connection.execute(
            f"""
            {_ROWS.format(index="INDEXED BY coverage_measurement_time")}
            WHERE m.heard_at <= :at AND (m.heard_at < :at OR m.id < :before)
              AND (:direct OR m.heard_how != 'direct')
            ORDER BY m.heard_at DESC, m.id DESC LIMIT :count
            """,
            {
                "at": 2**62 if before is None else before[0],
                "before": before_id or 0,
                "direct": direct,
                "count": count,
            },
        )
    ]
    checks = _checks(connection, rows)
    return SurveyCheckList(
        checks=tuple(check for check, _ in checks),
        oldest_at=from_epoch_ms(rows[-1]["heard_at"]) if rows else None,
        newest_at=from_epoch_ms(rows[0]["heard_at"]) if rows else None,
        next_before_id=int(rows[-1]["id"]) if len(rows) == count else None,
    )


@dataclass(frozen=True)
class _Scored:
    """A row's scoring, for its detail."""

    evidence: RelayEvidence
    score: MeasurementScore


def _checks(
    connection: sqlite3.Connection, rows: Sequence[Row]
) -> list[tuple[SurveyCheck, _Scored | None]]:
    """Each row's check, and its scoring when it was scored. Evidence is read
    per hour heard, with links around it, so a row scores as on its own."""
    hidden = opted_out(connection)
    receptions = survey_receptions(connection, rows)
    relayed = [row for row in rows if row["relay_byte"] and not is_direct(row)]
    candidates = {
        node
        for nodes in nodes_by_byte(connection, {int(row["relay_byte"]) for row in relayed}).values()
        for node in nodes
    }
    reach = own_reach(connection, candidates, from_km=NAME_KM) if candidates else {}
    hours: dict[int, list[Row]] = defaultdict(list)
    for row in relayed:
        hours[_hour(row)].append(row)
    scored: dict[int, _Scored] = {}
    for hour, its in hours.items():
        evidence = relay_evidence(connection, its, links_around=hour, reach=reach)
        for row in its:
            scored[int(row["id"])] = _Scored(
                evidence, score_measurement(connection, evidence, receptions, row)
            )
    todays = {int(row["id"]): _today(row, scored.get(int(row["id"]))) for row in rows}
    names = nodes_by_id(
        connection,
        {
            int(node)
            for row in rows
            for node in (
                row["sender_node_id"],
                row["receiver_node_id"],
                row["heard_node_id"],
                row["linked"],
                todays[int(row["id"])][0],
                todays[int(row["id"])][3],
            )
            if node is not None
        },
    )

    def node(node_id: int | None, name: str | None = None) -> RelayCheckNode | None:
        if node_id is None:
            return None
        if node_id in hidden:
            return RelayCheckNode(None, None)
        summary = names.get(node_id)
        known = None if summary is None else summary.long_name or summary.short_name
        return RelayCheckNode(node_id, known or name)

    checks = []
    for row in rows:
        found = scored.get(int(row["id"]))
        today = todays[int(row["id"])]
        saved = None if row["heard_how"] == "nearest" else row["heard_node_id"]
        linked = row["linked"]
        shown_how: HeardHow = (
            "survey_link"
            if linked is not None
            else "unknown"
            if row["heard_how"] == "nearest"
            else row["heard_how"]
        )
        sender = node(int(row["sender_node_id"]))
        surveyor = node(int(row["receiver_node_id"]), row["receiver_name"])
        assert sender is not None and surveyor is not None
        checks.append(
            (
                SurveyCheck(
                    measurement_id=int(row["id"]),
                    heard_at=from_epoch_ms(row["heard_at"]),
                    surveyor=surveyor,
                    sender=sender,
                    relay_byte=row["relay_byte"],
                    hop_start=row["hop_start"],
                    hop_limit=row["hop_limit"],
                    rx_snr=row["rx_snr"],
                    rx_rssi=row["rx_rssi"],
                    upload_node=node(row["heard_node_id"]),
                    upload_how=row["heard_how"],
                    link=node(linked),
                    link_nodes=0
                    if not row["relay_byte"] or row["heard_how"] == "direct"
                    else len(survey_candidates(receptions, row)),
                    today=SurveyToday(
                        node=node(today[0]),
                        how=today[1],
                        why=today[2],
                        likeliest=node(today[3]),
                        probability=today[4],
                        possible=0 if found is None else len(found.score.scores.terms),
                        ruled_out=0 if found is None else len(found.score.ruled_out),
                    ),
                    shown_node=node(linked if linked is not None else saved),
                    shown_how=shown_how,
                    changed=today[0] != saved,
                ),
                found,
            )
        )
    return checks


def _hour(row: Mapping[str, Any]) -> int:
    heard_at = int(row["heard_at"])
    return heard_at - heard_at % HOUR_MS


def _today(
    row: Mapping[str, Any], found: _Scored | None
) -> tuple[int | None, SavedHow, TodayWhy, int | None, float | None]:
    """Today's node, how and why, and the likeliest and its probability."""
    if is_direct(row):
        return int(row["sender_node_id"]), "direct", "direct", None, None
    if found is None:
        return None, "unknown", "no_relay_byte", None, None
    scores = found.score.scores
    ranked = scores.ranked()
    best = ranked[0] if ranked else None
    probability = None if best is None else round(scores.probabilities[best], 3)
    named = scores.named()
    if named is not None and named[1] != "receiver_heard":
        return named[0], named[1], "named", best, probability
    why: TodayWhy
    if best is None:
        why = "ruled_out" if found.score.ruled_out else "no_candidates"
    elif scores.probabilities[None] >= scores.probabilities[best]:
        why = "unknown"
    elif scores.probabilities[best] < NAME_PROBABILITY:
        why = "below"
    else:
        why = "no_evidence"
    return None, "unknown", why, best, probability


@dataclass(frozen=True)
class SurveyLinkNode:
    """A node sending the measurement's relay byte that some survey node on
    the same radio settings heard directly within the longest tier: how
    often, its reception nearest the phone (km) and, of those within
    link_km, the one nearest in time (hours from the measurement, negative
    before). tier_hours is the first tier that finds it, None when none
    does. strongest_here_db is the survey node's own strongest direct
    signal of it within too_strong_km, which too_strong compares.
    excluded: the measurement's sender or survey node, never its relay.
    candidate: survey evidence leaves it."""

    key: int
    node_id: int | None
    name: str | None
    receptions: int
    km: float | None
    hours: float | None
    tier_hours: float | None
    too_strong: bool
    strongest_here_db: float | None
    excluded: Literal["sender", "surveyor"] | None
    candidate: bool


@dataclass(frozen=True)
class SurveyLinkRules:
    km: float
    tiers_hours: tuple[float, ...]
    too_strong_db: float
    too_strong_km: float


@dataclass(frozen=True)
class SurveyCheckDetail:
    """One measurement's decisions, explained. The receiver is the phone
    where it heard the packet; reports are MQTT gateways' reports of the same
    packet (packet_row_id, the one pages show). candidates and their terms
    are today's rules', ruled_out the keys of those too_strong left out;
    signal_db the measurement's own signal as too_strong reads it."""

    check: SurveyCheck
    packet_row_id: int | None
    spreading_factor: int | None
    bandwidth_khz: float | None
    frequency_mhz: float | None
    one_hop: bool
    receiver: RelayPlace
    sender: RelayPlace | None
    sender_when_heard: RelayPlace | None
    route_places: tuple[RelayPlace, ...]
    candidates: tuple[RelayCandidate, ...]
    unknown_probability: float
    ruled_out: tuple[int, ...]
    reports: tuple[RelayCheckReport, ...]
    signals: tuple[RelaySignal, ...]
    links: tuple[RelayLinks, ...]
    signal_db: float | None
    survey_link: tuple[SurveyLinkNode, ...]
    activity_from: datetime | None
    rules: RelayRules
    link_rules: SurveyLinkRules


def survey_check(connection: sqlite3.Connection, measurement_id: int) -> SurveyCheckDetail | None:
    """One measurement's check, explained; None when there is none."""
    found_row = connection.execute(
        f"{_ROWS.format(index='')} WHERE m.id = ?", (measurement_id,)
    ).fetchone()
    if found_row is None:
        return None
    row = dict(found_row)
    [(check, found)] = _checks(connection, [row])
    hidden = opted_out(connection)
    sender = int(row["sender_node_id"])
    at = int(row["heard_at"])
    candidates: tuple[RelayCandidate, ...] = ()
    keys: dict[int, int] = {}
    explained: dict[str, Any] = {"signals": (), "links": ()}
    unknown = 1.0
    reports: list[RelayCheckReport] = []
    if found is not None:
        score = found.score
        candidates, keys = explain_candidates(found.evidence, score.copy, score.scores, {}, hidden)
        cohearers = set(score.copy.cohearers())
        reports = _reports(connection, row, score.copy, cohearers, hidden)
        explained = explain_evidence(
            found.evidence,
            score.copy,
            score.scores,
            keys,
            score.signals,
            [
                report.gateway.node_id
                for report in reports
                if report.signal and report.gateway.node_id
            ],
        )
        unknown = round(score.scores.probabilities[None], 4)
    shown_sender = sender not in hidden
    heard = positions_when_heard(connection, {(sender, at)}) if shown_sender else {}
    now = nodes_by_id(connection, {sender}).get(sender) if shown_sender else None
    when_heard = heard.get((sender, at))
    return SurveyCheckDetail(
        check=check,
        packet_row_id=shown_packet(connection, row),
        spreading_factor=row["spreading_factor"],
        bandwidth_khz=row["bandwidth_khz"],
        frequency_mhz=row["frequency_mhz"],
        one_hop=row["hop_start"] is not None
        and row["hop_limit"] is not None
        and row["hop_start"] - row["hop_limit"] == 1,
        receiver=RelayPlace(0, row["latitude"], row["longitude"], "phone"),
        sender=None
        if now is None or now.latitude is None or now.longitude is None
        else RelayPlace(
            sender, now.latitude, now.longitude, "estimated" if now.estimated else "now"
        ),
        sender_when_heard=None
        if when_heard is None
        else RelayPlace(sender, when_heard.latitude, when_heard.longitude, "when_heard"),
        route_places=(),
        candidates=candidates,
        unknown_probability=unknown,
        ruled_out=tuple(keys[node] for node in (found.score.ruled_out if found else ())),
        reports=tuple(reports),
        signals=explained["signals"],
        links=explained["links"],
        signal_db=signal_db(row["rx_rssi"], row["rx_snr"]),
        survey_link=_survey_link(connection, row, hidden),
        activity_from=None
        if found is None or found.evidence.history_from is None
        else from_epoch_ms(found.evidence.history_from),
        rules=RelayRules(
            name_probability=NAME_PROBABILITY,
            unknown_score=UNKNOWN_SCORE,
            name_km=NAME_KM,
            far_km=FAR_KM,
            active_hours=ACTIVE_MS / HOUR_MS,
            heard_days=COHEARER_WINDOW_MS / DAY_MS,
        ),
        link_rules=SurveyLinkRules(
            km=SURVEY_LINK_KM,
            tiers_hours=tuple(tier / HOUR_MS for tier in SURVEY_LINK_TIERS_MS),
            too_strong_db=TOO_STRONG_DB,
            too_strong_km=TOO_STRONG_KM,
        ),
    )


def _reports(
    connection: sqlite3.Connection,
    row: Row,
    copy: Relayed,
    cohearers: set[int],
    hidden: set[int],
) -> list[RelayCheckReport]:
    """The MQTT gateways' reports of the measurement's packet, each with its
    role for the phone's copy."""
    found = mqtt_reports(connection, row)
    gateways = {int(report["gateway_node_id"]) for report in found}
    at = int(row["heard_at"])
    heard = positions_when_heard(connection, {(gateway, at) for gateway in gateways})
    now = nodes_by_id(connection, gateways)
    sender = int(row["sender_node_id"])
    reports = []
    for report in found:
        gateway = int(report["gateway_node_id"])
        if gateway in hidden:
            continue
        place = heard.get((gateway, at)) or now.get(gateway)
        summary = now.get(gateway)
        kind: Literal["receiver", "sender", "cohearer", "earlier", "other"] = "other"
        if gateway == sender:
            kind = "sender"
        elif gateway in cohearers:
            kind = "cohearer"
        elif copy.hop_limit is not None and report["hop_limit"] == copy.hop_limit + 1:
            kind = "earlier"
        sends_byte = relay_byte_of(gateway) == copy.relay_byte
        header = SavedHeader(
            report["rx_snr"],
            report["rx_rssi"],
            report["hop_start"],
            report["hop_limit"],
            report["relay_byte"],
        )
        reports.append(
            RelayCheckReport(
                reception_id=int(report["id"]),
                gateway=RelayCheckNode(
                    gateway,
                    None if summary is None else summary.long_name or summary.short_name,
                ),
                latitude=None if place is None else place.latitude,
                longitude=None if place is None else place.longitude,
                hop_limit=report["hop_limit"],
                relay_byte=report["relay_byte"],
                snr_db=report["rx_snr"],
                rssi_dbm=report["rx_rssi"],
                direct=is_direct_reception(sender, gateway, header),
                kind=kind,
                sends_byte=sends_byte,
                signal=kind == "cohearer" and not sends_byte and report["rx_snr"] is not None,
                route_relay=None,
                route_hint=None,
                route_conflict=False,
            )
        )
    return reports


def _survey_link(
    connection: sqlite3.Connection, row: Row, hidden: set[int]
) -> tuple[SurveyLinkNode, ...]:
    """Each node survey links read for the row, nearest first."""
    if not row["relay_byte"] or is_direct(row):
        return ()
    heard = survey_receptions(connection, [row])
    key = (row["spreading_factor"], row["bandwidth_khz"], row["frequency_mhz"], row["relay_byte"])
    nodes = heard.get(key, {})
    found = set(survey_candidates(heard, row))
    names = nodes_by_id(connection, set(nodes))
    at, reach = int(row["heard_at"]), SURVEY_LINK_TIERS_MS[-1]
    position = (row["latitude"], row["longitude"])
    surveyor = int(row["receiver_node_id"])
    listed = []
    for index, (node, receptions) in enumerate(nodes.items()):
        near = [reception for reception in receptions if abs(reception[0] - at) <= reach]
        if not near:
            continue
        kms = [(great_circle_km(*position, r[1], r[2]), r) for r in near]
        close = [r for km, r in kms if km <= SURVEY_LINK_KM]
        here = [
            r[4] for km, r in kms if r[3] == surveyor and r[4] is not None and km <= TOO_STRONG_KM
        ]
        tier = heard_tier(receptions, row)
        summary = names.get(node)
        shown = node not in hidden
        listed.append(
            SurveyLinkNode(
                key=node if shown else -(index + 1),
                node_id=node if shown else None,
                name=None
                if summary is None or not shown
                else summary.long_name or summary.short_name,
                receptions=len(near),
                km=round(min(km for km, _ in kms), 1),
                hours=None
                if not close
                else round((min(close, key=lambda r: abs(r[0] - at))[0] - at) / HOUR_MS, 2),
                tier_hours=None if tier is None else SURVEY_LINK_TIERS_MS[tier] / HOUR_MS,
                too_strong=too_strong(receptions, row),
                strongest_here_db=max(here, default=None),
                excluded="sender"
                if node == int(row["sender_node_id"])
                else "surveyor"
                if node == surveyor
                else None,
                candidate=node in found,
            )
        )
    return tuple(sorted(listed, key=lambda item: (item.km is None, item.km, item.key)))
