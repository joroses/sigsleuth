"""Bounded reads for the operator diagnostics page."""

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from sigsleuth.config import Settings
from sigsleuth.db import (
    NodeSummary,
    facts_version,
    from_epoch_ms,
    nodes_by_id,
    optional_from_epoch_ms,
    reported_km,
    utc_now,
)
from sigsleuth.derive import MOVED_KM
from sigsleuth.links import (
    CONFIRMED_HOUR,
    LONG_LINK_KM,
    MAX_ESTIMATED_LINK_KM,
    PENDING_KM,
    SUSPECT_DISTANCE,
    TRUSTED_HOUR,
    Doubt,
    LinkKind,
    Misfit,
    doubtful_nodes,
    link_doubts,
    map_misfit,
)
from sigsleuth.mqtt import CaptureStatus
from sigsleuth.opt_out import opted_out, shown_sql
from sigsleuth.positions import direct_partners, doubt_limit_km, estimate_since_ms
from sigsleuth.radio import distance_km
from sigsleuth.terrain import LongLinkCheck

TABLES = ("reception", "packet", "node_current", "map_report")
# The one place skip and application reason codes are explained; the page
# shows these texts. Unusable reasons come from decode.parse_delivery.
REASON_DESCRIPTIONS = {
    # Unusable reasons: the delivery could not be grouped, so it was skipped.
    "invalid_service_envelope": (
        "The MQTT payload is not a readable service envelope: truncated or corrupted "
        "bytes, or not protobuf at all (for example JSON on the subscribed topic)."
    ),
    "missing_mesh_packet": "The service envelope opened, but held no mesh packet.",
    "missing_packet_identity": "The sender node number or packet ID is missing or 0.",
    "missing_packet_payload": "The packet has neither a decoded nor an encrypted payload.",
    "unrecognized_topic": (
        "The topic is not <root>/2/e/<channel>/<gateway> or <root>/e/<channel>/<gateway>, "
        "the envelope names no gateway, or the topic's gateway differs from the envelope's."
    ),
    "unrecognized_gateway": "The gateway ID is not a node ID of the form !aabbccdd.",
    "conflicting_channel_names": (
        "The topic and envelope name different channels; a bridge may have rewritten one."
    ),
    "missing_channel_name": "Neither the topic nor the envelope names a channel.",
    "decoder_failed": "SigSleuth's decoder crashed on this payload; the log has details.",
    "invalid_map_report": (
        "A delivery on <root>/2/map/ that is not a readable map report: no node "
        "number, an encrypted or empty packet, another port, or unreadable bytes."
    ),
    # Size limits: checked before parsing.
    "topic_too_large": "The MQTT topic is longer than 1,024 bytes.",
    "payload_too_large": "The MQTT payload is larger than 65,536 bytes.",
    "topic_and_payload_too_large": "Both the topic and the payload exceed their size limits.",
    # Unreadable reasons: the packet grouped, but its application data could not be read.
    "missing_application_payload": "The packet has no application payload to read.",
    "default_key_decryption_failed": (
        "The encrypted bytes did not open with the well-known default channel key; "
        "the channel probably uses a custom key."
    ),
    "invalid_node_info": "The NodeInfo payload is unreadable.",
    "invalid_position_payload": "The position payload is unreadable.",
    "invalid_route_payload": "The traceroute payload is unreadable.",
}


@dataclass(frozen=True)
class SkipCount:
    reason: str
    description: str
    count: int


@dataclass(frozen=True)
class DatabaseInfo:
    facts_version: int
    database_size_bytes: int
    wal_size_bytes: int
    row_counts: dict[str, int]
    oldest_packet_at: datetime | None
    newest_packet_at: datetime | None
    retention_days: int


@dataclass(frozen=True)
class DiagnosticsSummary:
    capture: CaptureStatus | None
    api_started_at: datetime
    api_uptime_seconds: float
    database: DatabaseInfo
    skipped: tuple[SkipCount, ...]


def diagnostics_summary(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    capture: CaptureStatus | None,
    api_started_at: datetime,
) -> DiagnosticsSummary:
    current_time = utc_now()
    # IDs grow with time, near enough, so the ends of the primary key give the
    # oldest and newest saved packets without an index on time.
    packet_range = connection.execute(
        """
        SELECT
            (SELECT first_received_at FROM packet ORDER BY id LIMIT 1),
            (SELECT last_received_at FROM packet ORDER BY id DESC LIMIT 1)
        """
    ).fetchone()
    row_counts = {
        table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in TABLES
    }
    database = DatabaseInfo(
        facts_version(connection),
        _file_size(settings.database_path),
        _file_size(Path(f"{settings.database_path}-wal")),
        row_counts,
        optional_from_epoch_ms(packet_range[0]),
        optional_from_epoch_ms(packet_range[1]),
        settings.retention_days,
    )
    return DiagnosticsSummary(
        capture=capture,
        api_started_at=api_started_at,
        api_uptime_seconds=max(0.0, (current_time - api_started_at).total_seconds()),
        database=database,
        skipped=_skip_counts(capture),
    )


def _skip_counts(capture: CaptureStatus | None) -> tuple[SkipCount, ...]:
    """Deliveries capture left unsaved since it started, most common first."""
    if capture is None:
        return ()
    return tuple(
        SkipCount(reason, describe_reason(reason), count)
        for reason, count in sorted(capture.skipped.items(), key=lambda item: -item[1])
    )


def describe_reason(code: str) -> str:
    return REASON_DESCRIPTIONS.get(code, "SigSleuth has no description for this code yet.")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


# The links Diagnostics looks through per page, by distance when heard
# (link_hour.km) over all saved history or by when last heard
# links.LONG_LINK_KM or more apart, before any rule hides one. A
# filtered page looks through LONG_LINK_SCAN at once, since few match; time a
# larger MAX_LONG_LINK_SCAN with perf.py before raising it.
LONGEST_LINKS = 30
LONG_LINK_SCAN = 300
MAX_LONG_LINK_SCAN = 1_000
# Warnings for reviewing them by hand, none of which the map acts on.
# A node whose nearest placed direct partner is farther than this (with at
# least two) may not be where it says; positions.doubt_limit_km judges the
# median instead.
LOCAL_PARTNER_KM = 60
# A node that has moved before, heard on a position at least this old, may
# have travelled again without reporting it.
STALE_POSITION_HOURS = 6
LINK_KINDS: tuple[LinkKind, ...] = ("direct", "neighbor", "traceroute")


@dataclass(frozen=True)
class LongLinkEnd:
    """One end of a long link as node_current has it now.

    altitude_meters 0 is how a manually set position says "not set".
    position_from_map_report marks a position from a map report, which may be
    rounded on purpose, rather than the node's own packet. moved_at is
    node_current's, doubtful node_doubtful's flag.
    direct_partners counts the placed nodes used to assess its current position
    (positions.direct_partners); nearest_partner_km and median_partner_km are over those.
    doubt_limit_km is the median beyond which its position is doubtful
    (positions.doubt_limit_km, from its altitude)."""

    node: NodeSummary
    altitude_meters: int | None
    position_observed_at: datetime | None
    position_from_map_report: bool
    moved_at: datetime | None
    doubtful: bool
    direct_partners: int
    nearest_partner_km: float | None
    median_partner_km: float | None
    doubt_limit_km: float


@dataclass(frozen=True)
class LongLinkEvidence:
    """One link kind's observations of a long link over saved history, every
    channel together. a_to_b counts node_a heard by node_b, b_to_a the reverse;
    hours counts the hours it was heard. zero_snr is true when every hour with
    an SNR added up to exactly 0 dB, which traceroutes report for a hop over
    MQTT. longest_km is its longest distance when heard. map_hidden_because is
    why the map, showing all history, leaves this kind of link out (the first
    channel's reason when there are several); None when it draws it."""

    link_kind: LinkKind
    a_to_b: int
    b_to_a: int
    hours: int
    average_snr: float | None
    zero_snr: bool
    first_hour_at: datetime
    last_seen_at: datetime
    longest_km: float | None
    map_hidden_because: str | None


@dataclass(frozen=True)
class LongLinkDoubt:
    """Why the Links page badges a long link and ranks it apart: 'position'
    when an end has a doubtful position, else 'terrain' when its newest km is
    LONG_LINK_KM or more on a pair the terrain check finds unlikely, or
    'signal' for a suspect signal distance. suspects
    are the ends to blame: the doubtful ones. The terrain check judges the pair,
    so for 'terrain' the suspect is a guess: the end in more unlikely pairs,
    when that is at least two, since one wrong position makes many; none when
    the ends are in as many."""

    reason: Literal["position", "terrain", "signal"]
    suspects: tuple[int, ...]


@dataclass(frozen=True)
class LongLink:
    """A link among the longest by distance when heard, every kind and channel
    together; node_a's ID is the lower.

    longest_at is when its longest hour was last heard, long_heard_at when
    its newest hour links.LONG_LINK_KM or more apart was (None when never
    that far). shortest_km and
    latest_km are its shortest and newest distances when heard, so a wide
    spread says an end moved or its position jumped; the Links page, showing
    all history, gives latest_km, and pending says it ranks that apart until
    confirmed (links.LinkRow). now_km is between the ends' current reported
    positions (db.reported_km) and drawn_km where the map draws them, estimates
    included. shown is what pages make of it now: 'long' when the map draws it,
    or the Links page ranks it unbadged, within derive.MOVED_KM of longest_km;
    'hidden' when the map leaves it out and the Links page badges it (doubt or
    pending); 'shorter' when they show a newer, shorter distance. doubt is why
    the Links page badges it doubtful, None when it does not. terrain is the terrain check of a
    link of links.LONG_LINK_KM or more (long_link), None when shorter or not
    judged yet. checks are warnings for a review by hand that the map's rules
    do not act on."""

    node_a: LongLinkEnd
    node_b: LongLinkEnd
    longest_km: float
    longest_at: datetime
    long_heard_at: datetime | None
    shortest_km: float
    latest_km: float | None
    pending: bool
    now_km: float | None
    drawn_km: float | None
    shown: Literal["long", "shorter", "hidden"]
    doubt: LongLinkDoubt | None
    channels: tuple[str, ...]
    evidence: tuple[LongLinkEvidence, ...]
    terrain: LongLinkCheck | None
    checks: tuple[str, ...]


@dataclass(frozen=True)
class LongLinkList:
    """The links that match a filter among those looked through, in the
    page's order. looked_through counts the links from the first to the last
    one looked through, and lowest_km is that one's distance (longest order)
    or oldest_hour_at the start of its newest long hour (latest order), so a
    filtered page can say how far down it looked. next_offset is where the
    next page starts looking, None when no link is left."""

    items: tuple[LongLink, ...]
    looked_through: int
    lowest_km: float | None
    oldest_hour_at: datetime | None
    next_offset: int | None


# What a page of long links can keep: links shown that way (LongLink.shown),
# badged for that reason (LongLinkDoubt.reason), judged unlikely by the
# terrain check whatever the pages make of it, pending, or with checks to
# review.
LongLinkFilter = Literal["long", "shorter", "hidden", "position", "unlikely", "pending", "checks"]
# How a page of long links is ranked: by longest distance when heard, or only
# the links heard links.LONG_LINK_KM or more apart, newest such hour first, to
# catch new ones. By hour, not last_seen_at: the hour is in link_hour_pair, so
# the table is never read (25 ms against 580 ms on perf.db, 2026-10-13).
LongLinkOrder = Literal["longest", "latest"]


def longest_links(
    connection: sqlite3.Connection,
    *,
    offset: int = 0,
    count: int = LONGEST_LINKS,
    only: LongLinkFilter | None = None,
    node: int | None = None,
    order: LongLinkOrder = "longest",
) -> LongLinkList:
    """Looks through the count links after the offset first in order over
    saved history, only those of node when given, whatever the map and Links
    page do with them, and keeps those matching only. Each comes with its
    evidence: where its ends are, what heard it, and whether the map draws
    it."""
    # Each direction's from the link_hour_pair index, then each pair's:
    # several times faster than grouping every hour by pair.
    rank, heard = (
        ("max(km)", "km IS NOT NULL")
        if order == "longest"
        else ("max(hour_start)", "km >= :long_km")
    )
    directions = (
        f"SELECT from_node_id AS a, to_node_id AS b, {rank} AS rank FROM link_hour"
        f" WHERE {heard} GROUP BY 1, 2"
        if node is None
        else f"SELECT from_node_id AS a, to_node_id AS b, {rank} AS rank FROM link_hour"
        f" WHERE from_node_id = :node AND {heard} GROUP BY 2"
        f" UNION ALL SELECT from_node_id, to_node_id, {rank} FROM link_hour"
        f" WHERE to_node_id = :node AND {heard} GROUP BY 1"
    )
    rows = connection.execute(
        f"""
        SELECT min(a, b) AS node_a, max(a, b) AS node_b, max(rank) FROM ({directions})
        GROUP BY 1, 2 HAVING {shown_sql("node_a")} AND {shown_sql("node_b")}
        ORDER BY 3 DESC, 1, 2 LIMIT :count OFFSET :offset
        """,
        {"node": node, "count": count + 1, "offset": offset, "long_km": LONG_LINK_KM},
    ).fetchall()
    looked = rows[:count]
    last = looked[-1][2] if looked else None
    links = _long_links(connection, [(int(a), int(b)) for a, b, _ in looked])
    return LongLinkList(
        items=tuple(link for link in links if only is None or _matches(link, only)),
        looked_through=offset + len(looked),
        lowest_km=last if order == "longest" else None,
        oldest_hour_at=optional_from_epoch_ms(last) if order == "latest" else None,
        next_offset=offset + count if len(rows) > count else None,
    )


def _matches(link: LongLink, only: LongLinkFilter) -> bool:
    if only in ("long", "shorter", "hidden"):
        return link.shown == only
    if only == "unlikely":
        return link.terrain is not None and link.terrain.unlikely
    if only == "checks":
        return bool(link.checks)
    if only == "pending":
        return link.pending
    return link.doubt is not None and link.doubt.reason == only


def _long_links(connection: sqlite3.Connection, pairs: list[tuple[int, int]]) -> list[LongLink]:
    """The links of these node pairs (lower ID first), in the same order,
    leaving out a pair with no hour that has a distance."""
    if not pairs:
        return []
    ids = sorted({node for pair in pairs for node in pair})
    hours: dict[tuple[int, int], list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        f"""
        WITH heard (from_node_id, to_node_id) AS (
            SELECT value ->> 0, value ->> 1 FROM json_each(:pairs)
            UNION ALL SELECT value ->> 1, value ->> 0 FROM json_each(:pairs)
        )
        SELECT link_hour.*, {TRUSTED_HOUR} AS trusted,
               {SUSPECT_DISTANCE} AS suspect_distance,
               km >= :pending_km AND {CONFIRMED_HOUR} AS confirmed
        FROM heard JOIN link_hour USING (from_node_id, to_node_id)
        """,
        {"pairs": json.dumps(pairs), "since": 0, "pending_km": PENDING_KM},
    ):
        heard = (row["from_node_id"], row["to_node_id"])
        hours[min(heard), max(heard)].append(row)
    pairs = [pair for pair in pairs if any(row["km"] is not None for row in hours[pair])]
    ends = _long_link_ends(connection, ids)
    # long_link is small: a row per pair heard 250 km or more.
    unlikely_pairs = Counter(
        node
        for pair in connection.execute("SELECT node_a, node_b FROM long_link WHERE unlikely")
        for node in pair
    )
    checks = {
        (int(row["node_a"]), int(row["node_b"])): LongLinkCheck(
            row["sea_share"], row["above_meters"], bool(row["confirmed"]), bool(row["unlikely"])
        )
        for row in connection.execute(
            """
            SELECT * FROM long_link
            WHERE node_a IN (SELECT value FROM json_each(:ids))
              AND node_b IN (SELECT value FROM json_each(:ids))
            """,
            {"ids": json.dumps(ids)},
        )
    }
    return [
        _long_link(ends[a], ends[b], hours[a, b], checks.get((a, b)), unlikely_pairs)
        for a, b in pairs
    ]


@dataclass(frozen=True)
class DoubtfulNode:
    """A node to look at when reviewing long links: one with a doubtful
    position (node_doubtful), or one in at least two pairs the terrain check
    finds unlikely, which is how LongLinkDoubt guesses whom to blame.
    long_links counts its pairs heard links.LONG_LINK_KM or more apart
    (long_link), unlikely_links those found unlikely."""

    end: LongLinkEnd
    long_links: int
    unlikely_links: int


@dataclass(frozen=True)
class DoubtfulNodeList:
    items: tuple[DoubtfulNode, ...]


def doubtful_node_list(connection: sqlite3.Connection) -> DoubtfulNodeList:
    """Every doubtful or often-unlikely node: doubtful first, then most
    unlikely links. Both sources are small: tens of nodes."""
    long_links: Counter[int] = Counter()
    unlikely: Counter[int] = Counter()
    for a, b, is_unlikely in connection.execute("SELECT node_a, node_b, unlikely FROM long_link"):
        for node in (int(a), int(b)):
            long_links[node] += 1
            unlikely[node] += is_unlikely
    ids = doubtful_nodes(connection) | {node for node, n in unlikely.items() if n >= 2}
    ids -= opted_out(connection)
    ends = _long_link_ends(connection, sorted(ids))
    return DoubtfulNodeList(
        tuple(
            sorted(
                (DoubtfulNode(ends[node], long_links[node], unlikely[node]) for node in ids),
                key=lambda item: (
                    not item.end.doubtful,
                    -item.unlikely_links,
                    -item.long_links,
                    item.end.node.node_id,
                ),
            )
        )
    )


def _long_link_ends(connection: sqlite3.Connection, ids: list[int]) -> dict[int, LongLinkEnd]:
    nodes = nodes_by_id(connection, set(ids))
    current = {
        int(row["node_id"]): row
        for row in connection.execute(
            """
            SELECT node_id, altitude_meters, position_observed_at, position_packet_row_id,
                   latitude, moved_at
            FROM node_current WHERE node_id IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(ids),),
        )
    }
    doubtful = doubtful_nodes(connection)
    partners = direct_partners(connection, estimate_since_ms(connection), ids)
    ends = {}
    for node_id in ids:
        row = current.get(node_id)
        found = sorted(partners.get(node_id, {}).values())
        ends[node_id] = LongLinkEnd(
            node=nodes.get(node_id)
            or NodeSummary(node_id, None, None, None, None, None, estimated=False),
            altitude_meters=None if row is None else row["altitude_meters"],
            position_observed_at=None
            if row is None
            else optional_from_epoch_ms(row["position_observed_at"]),
            position_from_map_report=row is not None
            and row["latitude"] is not None
            and row["position_packet_row_id"] is None,
            moved_at=None if row is None else optional_from_epoch_ms(row["moved_at"]),
            doubtful=node_id in doubtful,
            direct_partners=len(found),
            nearest_partner_km=found[0] if found else None,
            median_partner_km=round(statistics.median(found), 1) if found else None,
            doubt_limit_km=round(doubt_limit_km(None if row is None else row["altitude_meters"])),
        )
    return ends


def _long_link(
    a: LongLinkEnd,
    b: LongLinkEnd,
    rows: Sequence[sqlite3.Row],
    terrain: LongLinkCheck | None,
    unlikely_pairs: Mapping[int, int],
) -> LongLink:
    measured = [row for row in rows if row["km"] is not None]
    # Among hours as long, the latest, whatever order they were read in.
    longest = max(measured, key=lambda row: (row["km"], row["last_seen_at"]))
    newest = max(rows, key=lambda row: row["last_seen_at"])
    long_hours = [row for row in measured if row["km"] >= LONG_LINK_KM]
    longest_km = longest["km"]
    unlikely = terrain is not None and terrain.unlikely
    evidence = tuple(
        _evidence(kind, kind_rows, a, b, unlikely)
        for kind in LINK_KINDS
        if (kind_rows := [row for row in rows if row["link_kind"] == kind])
    )
    drawn_km = distance_km(a.node, b.node)
    drawn = any(kind.map_hidden_because is None for kind in evidence)
    # As the Links page badges it, over all history (links.link_table).
    doubts = link_doubts(
        a.doubtful or b.doubtful,
        newest["km"],
        unlikely,
        any(row["confirmed"] for row in rows),
        bool(newest["suspect_distance"]),
    )
    doubt = _doubt(a, b, doubts, unlikely_pairs)
    pending = "pending" in doubts

    def still_long(km: float | None) -> bool:
        return km is not None and abs(longest_km - km) <= MOVED_KM

    shown: Literal["long", "shorter", "hidden"] = "shorter"
    if (drawn and still_long(drawn_km)) or (
        doubt is None and not pending and still_long(newest["km"])
    ):
        shown = "long"
    elif not drawn and (doubt is not None or pending):
        shown = "hidden"
    link = LongLink(
        node_a=a,
        node_b=b,
        longest_km=longest_km,
        longest_at=from_epoch_ms(longest["last_seen_at"]),
        long_heard_at=from_epoch_ms(max(row["last_seen_at"] for row in long_hours))
        if long_hours
        else None,
        shortest_km=min(row["km"] for row in measured),
        latest_km=newest["km"],
        pending=pending,
        now_km=reported_km(a.node, b.node),
        drawn_km=drawn_km,
        shown=shown,
        doubt=doubt,
        channels=tuple(sorted({str(row["channel"]) for row in rows})),
        evidence=evidence,
        terrain=terrain if longest_km >= LONG_LINK_KM else None,
        checks=(),
    )
    return LongLink(**{**vars(link), "checks": _checks(link)})


def _doubt(
    a: LongLinkEnd, b: LongLinkEnd, doubts: Sequence[Doubt], unlikely_pairs: Mapping[int, int]
) -> LongLinkDoubt | None:
    ends = (a, b)
    if "doubtful" in doubts:
        return LongLinkDoubt("position", tuple(end.node.node_id for end in ends if end.doubtful))
    if "signal" in doubts:
        return LongLinkDoubt("signal", ())
    if "terrain" not in doubts:
        return None
    more, fewer = sorted((end.node.node_id for end in ends), key=lambda node: -unlikely_pairs[node])
    if unlikely_pairs[more] >= 2 and unlikely_pairs[more] > unlikely_pairs[fewer]:
        return LongLinkDoubt("terrain", (more,))
    return LongLinkDoubt("terrain", ())


def _evidence(
    kind: LinkKind,
    rows: Sequence[sqlite3.Row],
    a: LongLinkEnd,
    b: LongLinkEnd,
    unlikely: bool,
) -> LongLinkEvidence:
    snr_count = sum(row["snr_count"] for row in rows)
    snr_sum = sum(row["snr_sum"] for row in rows)
    measured = [row["km"] for row in rows if row["km"] is not None]
    return LongLinkEvidence(
        link_kind=kind,
        a_to_b=sum(row["observations"] for row in rows if row["from_node_id"] == a.node.node_id),
        b_to_a=sum(row["observations"] for row in rows if row["from_node_id"] == b.node.node_id),
        hours=len({row["hour_start"] for row in rows}),
        average_snr=round(snr_sum / snr_count, 2) if snr_count else None,
        zero_snr=snr_count > 0 and all(row["snr_sum"] == 0 for row in rows if row["snr_count"]),
        first_hour_at=from_epoch_ms(min(row["hour_start"] for row in rows)),
        last_seen_at=from_epoch_ms(max(row["last_seen_at"] for row in rows)),
        longest_km=max(measured) if measured else None,
        map_hidden_because=_map_hidden_because(rows, a, b, unlikely),
    )


def _map_hidden_because(
    rows: Sequence[sqlite3.Row],
    a: LongLinkEnd,
    b: LongLinkEnd,
    unlikely: bool,
) -> str | None:
    """Why the map with all history shown leaves out one kind of link, by its
    own rules: the hours it trusts (links.TRUSTED_HOUR), then whether the newest
    of them fits where it draws the ends, given the pair's terrain check
    (links.map_misfit). None when it draws the link on any channel."""
    reasons = []
    for channel in sorted({row["channel"] for row in rows}):
        heard = [row for row in rows if row["channel"] == channel]
        trusted = [row for row in heard if row["trusted"]]
        if not trusted:
            if all(row["suspect_distance"] for row in heard):
                reasons.append("the signal suggests a suspect distance")
            else:
                reasons.append(_distrusted_because(a, b, max(row["last_seen_at"] for row in heard)))
            continue
        newest = max(trusted, key=lambda row: row["last_seen_at"])
        misfit = map_misfit(a.node, b.node, newest["km"], unlikely)
        if misfit is None:
            return None
        reasons.append(_misfit_because(misfit, a, b, newest["km"]))
    return reasons[0]


def _misfit_because(misfit: Misfit, a: LongLinkEnd, b: LongLinkEnd, heard_km: float | None) -> str:
    """links.map_misfit's reason in words, for the map with no max_km."""
    drawn = distance_km(a.node, b.node)
    if misfit == "terrain":
        return (
            f"it was heard {heard_km:g} km apart, mostly over land with no end high "
            "above the land between, or over too much land: the terrain check finds "
            "that unlikely"
        )
    if misfit == "estimated_far":
        return f"an estimated end is drawn {drawn:g} km away, over {MAX_ESTIMATED_LINK_KM} km"
    if misfit == "unmeasured":
        return (
            f"its ends are drawn {drawn:g} km apart, but it was heard before one of "
            "them had a position, so nothing can check it"
        )
    if misfit == "drifted":
        return (
            f"its ends are drawn {drawn:g} km apart but were {heard_km:g} km apart when last heard"
        )
    return f"its ends are drawn {drawn:g} km apart, too far for this page"


def _distrusted_because(a: LongLinkEnd, b: LongLinkEnd, last_seen_ms: int) -> str:
    for end in (a, b):
        if end.doubtful:
            return f"{_name(end)} has a doubtful position"
    last_seen = from_epoch_ms(last_seen_ms)
    for end in (a, b):
        if end.moved_at is not None and last_seen <= end.moved_at:
            return f"it was heard before {_name(end)} last moved"
    return "it was heard before an end last moved"


def _checks(link: LongLink) -> tuple[str, ...]:
    checks = []
    ends = (link.node_a, link.node_b)
    if link.terrain is None and link.longest_km >= LONG_LINK_KM:
        checks.append(
            "Not checked against terrain yet: the check runs every few minutes "
            "and needs terrain tiles."
        )
    if link.longest_km - link.shortest_km > MOVED_KM:
        checks.append(
            f"Its distance when heard ranged {link.shortest_km:g}-{link.longest_km:g} km: "
            "an end moved or its position jumped while it was heard."
        )
    kinds = {evidence.link_kind: evidence for evidence in link.evidence}
    if sum(evidence.a_to_b + evidence.b_to_a for evidence in link.evidence) == 1:
        checks.append("Heard only once.")
    if "direct" not in kinds and "neighbor" not in kinds:
        checks.append("Only traceroutes report it: no direct reception or neighbour report.")
    if "traceroute" in kinds and kinds["traceroute"].zero_snr:
        checks.append("Every route step's SNR is exactly 0 dB, the mark of a hop over MQTT.")
    for end in ends:
        name = _name(end)
        if end.direct_partners >= 2 and (end.nearest_partner_km or 0) > LOCAL_PARTNER_KM:
            checks.append(
                f"{name}'s nearest of {end.direct_partners} direct partners was "
                f"{end.nearest_partner_km:g} km away: its position may be wrong."
            )
        if end.position_from_map_report:
            checks.append(f"{name}'s position is from a map report, which may be rounded.")
        if (
            end.moved_at is not None
            and end.moved_at <= link.longest_at
            and end.position_observed_at is not None
            and end.position_observed_at <= link.longest_at
            and (link.longest_at - end.position_observed_at).total_seconds()
            > STALE_POSITION_HOURS * 3600
        ):
            age = (link.longest_at - end.position_observed_at).total_seconds() / 3600
            checks.append(
                f"{name} has moved before and its position was {age:.0f} h old when heard: "
                "it may have travelled again."
            )
    return tuple(checks)


def _name(end: LongLinkEnd) -> str:
    node = end.node
    return node.long_name or node.short_name or f"!{node.node_id:08x}"
