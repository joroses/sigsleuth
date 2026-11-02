"""Bounded reads for the operator diagnostics page."""

import base64
import bisect
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

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
from sigsleuth.derive import HOUR_MS, MOVED_KM
from sigsleuth.links import (
    CONFIRMED_HOUR,
    LONG_LINK_KM,
    MAX_ESTIMATED_LINK_KM,
    SUSPECT_DISTANCE,
    SUSPECT_MIN_KM,
    SUSPECT_POWER_DBM,
    TRUSTED_HOUR,
    UNCONFIRMED_KM,
    Doubt,
    LinkKind,
    Misfit,
    doubtful_nodes,
    link_doubts,
    map_misfit,
)
from sigsleuth.mqtt import CaptureStatus
from sigsleuth.opt_out import opted_out, shown_sql, unlinkable_packets
from sigsleuth.positions import (
    DOUBTFUL_PARTNER_KM,
    ESTIMATE_DAYS,
    direct_partners,
    doubt_limit_km,
    estimate_since_ms,
)
from sigsleuth.radio import distance_km, great_circle_km
from sigsleuth.shared_numbers import shared_nodes
from sigsleuth.terrain import (
    ABOVE_LAND_M,
    CONFIRMING_PARTNER_KM,
    MAX_LAND_KM,
    SEA_SHARE,
    LongLinkCheck,
)

TABLES = ("reception", "packet", "node_current", "map_report")


@dataclass(frozen=True)
class DeviceReport:
    """One MAC/key profile: its latest names and packet, its first report,
    and the other node numbers its MAC reported under."""

    long_name: str | None
    short_name: str | None
    hardware_model: str | None
    mac_address: str | None
    public_key: str | None
    first_observed_at: datetime
    observed_at: datetime
    packet_row_id: int
    other_node_ids: tuple[int, ...]


@dataclass(frozen=True)
class SharedProof:
    """Why a number is shared (shared_numbers.py): other_packet_row_id, one
    radio's NodeInfo heard at proof_at, came inside another radio's counter
    run from run_from_ to run_to_packet_row_id. None for an expired packet."""

    proof_at: datetime
    run_from_packet_row_id: int | None
    run_to_packet_row_id: int | None
    other_packet_row_id: int | None


@dataclass(frozen=True)
class DeviceConflict:
    """kind is 'shared' when two radios were on air at once, 'one_at_a_time'
    for two or more radios without that proof, None when only keys differ."""

    node_id: int
    kind: Literal["shared", "one_at_a_time"] | None
    proof: SharedProof | None
    reports: tuple[DeviceReport, ...]
    limited: bool


DeviceConflictFilter = Literal["shared", "one_at_a_time", "keys"]


@dataclass(frozen=True)
class DeviceConflictList:
    """Counts span all retained IDs, including other pages and cases."""

    items: tuple[DeviceConflict, ...]
    next_node_id: int | None
    counts: dict[DeviceConflictFilter, int]


def device_conflicts(
    connection: sqlite3.Connection,
    *,
    after_node: int = 0,
    count: int = 30,
    only: DeviceConflictFilter | None = None,
) -> DeviceConflictList:
    """IDs with differing nonempty MACs or keys in retained NodeInfo reports.
    A change can be a replacement or key reset; these are reports to inspect,
    not a count of physical devices. Each ID shows its newest 20 profiles."""
    shown = f"{shown_sql('d.node_id')} AND {shown_sql('p.recipient_node_id')}"
    nodes = connection.execute(
        f"""
        SELECT d.node_id,
               CASE WHEN s.node_id IS NOT NULL THEN 'shared'
                    WHEN count(DISTINCT nullif(d.mac_address, x'')) > 1 THEN 'one_at_a_time'
                    ELSE NULL END AS kind
        FROM node_device AS d JOIN packet AS p ON p.id = d.packet_row_id
        LEFT JOIN node_shared AS s ON s.node_id = d.node_id
        WHERE {shown}
        GROUP BY d.node_id
        HAVING (count(DISTINCT nullif(d.mac_address, x'')) > 1
            OR count(DISTINCT nullif(d.public_key, x'')) > 1)
        ORDER BY d.node_id
        """,
    ).fetchall()
    totals = Counter(row["kind"] or "keys" for row in nodes)
    counts: dict[DeviceConflictFilter, int] = {
        "shared": totals["shared"],
        "one_at_a_time": totals["one_at_a_time"],
        "keys": totals["keys"],
    }
    nodes = [
        row
        for row in nodes
        if row["node_id"] > after_node and (only is None or (row["kind"] or "keys") == only)
    ][: count + 1]
    hidden = opted_out(connection)
    items = []
    for node in nodes[:count]:
        rows = connection.execute(
            f"""
            SELECT d.* FROM node_device AS d JOIN packet AS p ON p.id = d.packet_row_id
            WHERE d.node_id = ? AND {shown}
            ORDER BY d.observed_at DESC, d.packet_row_id DESC LIMIT 21
            """,
            (node["node_id"],),
        ).fetchall()
        node_id = int(node["node_id"])
        reports = tuple(
            DeviceReport(
                row["long_name"],
                row["short_name"],
                row["hardware_model"],
                bytes(row["mac_address"]).hex(":") or None,
                base64.b64encode(row["public_key"]).decode() or None,
                from_epoch_ms(row["first_observed_at"]),
                from_epoch_ms(row["observed_at"]),
                row["packet_row_id"],
                _other_numbers(connection, node_id, bytes(row["mac_address"]), hidden),
            )
            for row in rows[:20]
        )
        proof = _shared_proof(connection, node_id)
        items.append(DeviceConflict(node_id, node["kind"], proof, reports, len(rows) > 20))
    return DeviceConflictList(
        tuple(items), int(nodes[count - 1]["node_id"]) if len(nodes) > count else None, counts
    )


def _other_numbers(
    connection: sqlite3.Connection, node_id: int, mac: bytes, hidden: set[int]
) -> tuple[int, ...]:
    if not mac:
        return ()
    return tuple(
        int(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT node_id FROM node_device INDEXED BY node_device_mac
            WHERE mac_address = ? AND mac_address != x'' AND node_id != ?
            ORDER BY node_id
            """,
            (mac, node_id),
        )
        if int(row[0]) not in hidden
    )


def _shared_proof(connection: sqlite3.Connection, node_id: int) -> SharedProof | None:
    row = connection.execute("SELECT * FROM node_shared WHERE node_id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    packets = (
        row["run_from_packet_row_id"],
        row["run_to_packet_row_id"],
        row["other_packet_row_id"],
    )
    unshown = unlinkable_packets(connection, packets)
    return SharedProof(
        from_epoch_ms(row["proof_at"]),
        *(None if packet in unshown else int(packet) for packet in packets),
    )


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
    revision: str | None
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
    revision: str | None,
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
        revision=revision,
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
# Review unusually long contacts without rejecting exceptional radio paths:
# a link REACH_REVIEW_KM or longer is compared with each end's threshold,
# REACH_REVIEW_KM + REACH_FACTOR x (median + IQR) of its other partners'
# distances, once it has REACH_PARTNERS of them in REACH_HOURS hours on
# REACH_DAYS days, starting a day before.
# Revisit 2026-11-02: on perf.db (14 days of loranet.pl), flagged 20 of 22
# pairs judged wrong by hand and none of those judged real; early exceptional
# contacts still need review.
REACH_REVIEW_KM = 150
REACH_FACTOR = 3
REACH_PARTNERS = 3
REACH_HOURS = 3
REACH_DAYS = 2
DAY_MS = 24 * HOUR_MS
LINK_KINDS: tuple[LinkKind, ...] = ("direct", "neighbor", "traceroute")


@dataclass(frozen=True)
class LongLinkEnd:
    """One end of a long link as node_current has it now.

    altitude_meters 0 is how a manually set position says "not set".
    position_from_map_report marks a position from a map report, which may be
    rounded on purpose, rather than the node's own packet. moved_at is
    node_current's, doubtful node_doubtful's flag, shared whether it is a
    shared node number (shared_numbers.py).
    direct_partners counts the placed nodes used to assess its current position
    (positions.direct_partners); nearest_partner_km and median_partner_km are over those.
    blamed_partners counts far partners excluded because their other partners
    contradict their positions.
    traceroute_corroborated means measured hops support its position around
    every counted far direct hour, despite its direct median.
    doubt_limit_km is the altitude-aware direct median limit, before corroboration;
    over_limit compares the unrounded median with it, as positions does."""

    node: NodeSummary
    altitude_meters: int | None
    position_observed_at: datetime | None
    position_from_map_report: bool
    moved_at: datetime | None
    doubtful: bool
    shared: bool
    direct_partners: int
    blamed_partners: int
    traceroute_corroborated: bool
    nearest_partner_km: float | None
    median_partner_km: float | None
    doubt_limit_km: float
    over_limit: bool


@dataclass(frozen=True)
class LongLinkEvidence:
    """One link kind's observations of a long link over saved history, every
    channel together. a_to_b counts node_a heard by node_b, b_to_a the reverse;
    hours counts the hours it was heard. zero_snr is true when every hour with
    an SNR added up to exactly 0 dB, which traceroutes report for a hop over
    MQTT. longest_km is its longest distance when heard. map_hidden_because is
    why the map, showing all history, leaves this kind of link out (the first
    channel's reason when there are several); None when it draws it. doubts are
    the badges the Links page gives its rows of this kind (links.link_doubts).
    """

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
    doubts: tuple[Doubt, ...]


@dataclass(frozen=True)
class LongLinkDoubt:
    """Why the Links page badges a long link and ranks it apart: 'shared'
    when an end is a shared node number, else 'position'
    when an end has a doubtful position, else 'terrain' when its row's newest
    km is LONG_LINK_KM or more on a pair the terrain check finds unlikely, or
    'signal' for a suspect signal distance. suspects
    are the ends to blame: the shared or doubtful ones. The terrain check judges the pair,
    so for 'terrain' the suspect is a guess: the end in more unlikely pairs,
    when that is at least two, since one wrong position makes many; none when
    the ends are in as many."""

    reason: Literal["shared", "position", "terrain", "signal"]
    suspects: tuple[int, ...]


# Warnings for a review by hand (ReviewCheck).
CheckKind = Literal[
    "distance_range",
    "heard_once",
    "traceroute_only",
    "mqtt_hop",
    "far_partners",
    "map_report",
    "stale_position",
    "terrain_unchecked",
    "reach_long",
    "reach_unknown",
]


@dataclass(frozen=True)
class ReviewCheck:
    """A warning for a review by hand that the map's rules do not act on;
    node_id is the end it is about, None when it is about the link."""

    kind: CheckKind
    node_id: int | None = None


@dataclass(frozen=True)
class ReachProfile:
    """One end's earlier contacts with other partners and the review threshold
    they give; None when its history is too sparse."""

    node_id: int
    partners: int
    hours: int
    days: int
    first_hour_at: datetime | None
    last_seen_at: datetime | None
    median_km: float | None
    iqr_km: float | None
    threshold_km: float | None


@dataclass(frozen=True)
class ReachComparison:
    link_kind: LinkKind
    channel: str
    before_at: datetime
    ends: tuple[ReachProfile, ReachProfile]


@dataclass(frozen=True)
class LongLink:
    """A link among the longest by distance when heard, every kind and channel
    together; node_a's ID is the lower.

    longest_at is when its longest hour was last heard, long_heard_at when
    its newest hour links.LONG_LINK_KM or more apart was (None when never
    that far). shortest_km is its shortest distance when heard, so a wide
    spread says an end moved or its position jumped. The Links page shows a row
    per kind and channel (links.LinkRow); the row holding the longest hour,
    showing all history, gives latest_km, doubt and unconfirmed. now_km is
    between the ends' current reported positions (db.reported_km) and drawn_km
    where the map draws them, estimates included. shown is what pages make of
    it now: 'long' when the map draws it, or that row is unbadged, within
    derive.MOVED_KM of longest_km; 'hidden' when the map leaves it out and that
    row is badged (doubt or unconfirmed); 'shorter' when they show a newer,
    shorter distance. doubt is why that row is badged doubtful, None when it is
    not. terrain is the terrain check of a link of links.LONG_LINK_KM or more
    (long_link), None when shorter or not judged yet. checks are warnings for a
    review by hand that the map's rules do not act on. reach compares other
    contacts before the longest hour."""

    node_a: LongLinkEnd
    node_b: LongLinkEnd
    longest_km: float
    longest_at: datetime
    long_heard_at: datetime | None
    shortest_km: float
    latest_km: float | None
    unconfirmed: bool
    now_km: float | None
    drawn_km: float | None
    shown: Literal["long", "shorter", "hidden"]
    doubt: LongLinkDoubt | None
    channels: tuple[str, ...]
    evidence: tuple[LongLinkEvidence, ...]
    terrain: LongLinkCheck | None
    checks: tuple[ReviewCheck, ...]
    reach: ReachComparison | None


@dataclass(frozen=True)
class LongLinkRules:
    """The thresholds behind long links' flags and checks, so the page can
    say how each is decided."""

    long_link_km: float = LONG_LINK_KM
    unconfirmed_km: float = UNCONFIRMED_KM
    moved_km: float = MOVED_KM
    partner_days: int = ESTIMATE_DAYS
    doubtful_partner_km: float = DOUBTFUL_PARTNER_KM
    local_partner_km: float = LOCAL_PARTNER_KM
    stale_position_hours: float = STALE_POSITION_HOURS
    terrain_above_meters: float = ABOVE_LAND_M
    terrain_sea_share: float = SEA_SHARE
    terrain_max_land_km: float = MAX_LAND_KM
    confirming_partner_km: float = CONFIRMING_PARTNER_KM
    signal_min_km: float = SUSPECT_MIN_KM
    signal_power_dbm: float = SUSPECT_POWER_DBM
    reach_review_km: float = REACH_REVIEW_KM
    reach_factor: float = REACH_FACTOR
    reach_partners: int = REACH_PARTNERS
    reach_hours: int = REACH_HOURS
    reach_days: int = REACH_DAYS


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
    rules: LongLinkRules


# What a page of long links can keep: links shown that way (LongLink.shown),
# badged for that reason (LongLinkDoubt.reason), judged unlikely by the
# terrain check whatever the pages make of it, unconfirmed, or with checks to
# review.
LongLinkFilter = Literal[
    "long", "shorter", "hidden", "shared", "position", "unlikely", "signal", "unconfirmed", "checks"
]
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
        rules=LongLinkRules(),
    )


def _matches(link: LongLink, only: LongLinkFilter) -> bool:
    if only in ("long", "shorter", "hidden"):
        return link.shown == only
    if only == "unlikely":
        return link.terrain is not None and link.terrain.unlikely
    if only == "checks":
        return bool(link.checks)
    if only == "unconfirmed":
        return link.unconfirmed
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
               km >= :unconfirmed_km AND {CONFIRMED_HOUR} AS confirmed
        FROM heard JOIN link_hour USING (from_node_id, to_node_id)
        """,
        {"pairs": json.dumps(pairs), "since": 0, "unconfirmed_km": UNCONFIRMED_KM},
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
    reach = _reach_comparisons(connection, hours)
    result = []
    for a, b in pairs:
        link = _long_link(ends[a], ends[b], hours[a, b], checks.get((a, b)), unlikely_pairs)
        comparison = reach.get((a, b))
        warnings: tuple[ReviewCheck, ...] = ()
        if comparison is not None:
            thresholds = [
                end.threshold_km for end in comparison.ends if end.threshold_km is not None
            ]
            if not thresholds:
                warnings = (ReviewCheck("reach_unknown"),)
            elif link.longest_km > max(thresholds):
                warnings = (ReviewCheck("reach_long"),)
        result.append(replace(link, reach=comparison, checks=link.checks + warnings))
    return result


def _reach_comparisons(
    connection: sqlite3.Connection, hours: Mapping[tuple[int, int], Sequence[sqlite3.Row]]
) -> dict[tuple[int, int], ReachComparison]:
    """A ReachComparison for each pair whose longest hour is REACH_REVIEW_KM or more."""
    targets = {}
    for pair, rows in hours.items():
        measured = [row for row in rows if row["km"] is not None]
        if measured:
            longest = max(measured, key=lambda row: (row["km"], row["last_seen_at"]))
            if longest["km"] >= REACH_REVIEW_KM:
                targets[pair] = longest
    if not targets:
        return {}
    ids = sorted({n for pair in targets for n in pair})
    profiles: dict[tuple[int, str, str], int] = {}
    for pair, row in targets.items():
        for n in pair:
            key = (n, row["link_kind"], row["channel"])
            profiles[key] = max(profiles.get(key, 0), row["hour_start"])
    refs: dict[tuple[int, str, str], list[tuple[int, int, int, float]]] = defaultdict(list)
    # Discover incoming partners on the covering pair index, then seek their hours.
    for row in connection.execute(
        f"""
        WITH peers AS MATERIALIZED (
            SELECT DISTINCT from_node_id, to_node_id FROM link_hour INDEXED BY link_hour_pair
            WHERE (from_node_id IN (SELECT value FROM json_each(:ids))
                OR to_node_id IN (SELECT value FROM json_each(:ids)))
              AND from_node_id != 0 AND to_node_id != 0 AND from_node_id != to_node_id
              AND {shown_sql("from_node_id")} AND {shown_sql("to_node_id")}
        )
        , contexts AS MATERIALIZED (
            SELECT value ->> 0 AS node, value ->> 1 AS kind, value ->> 2 AS channel,
                   value ->> 3 AS cutoff FROM json_each(:profiles)
        )
        SELECT c.node, h.from_node_id, h.to_node_id, h.link_kind, h.channel,
               h.hour_start, h.last_seen_at, h.km
        FROM peers CROSS JOIN contexts c CROSS JOIN link_hour h INDEXED BY link_hour_pair
        WHERE h.from_node_id = peers.from_node_id AND h.to_node_id = peers.to_node_id
          AND (c.node = peers.from_node_id OR c.node = peers.to_node_id)
          AND h.link_kind = c.kind AND h.channel = c.channel
          AND h.km IS NOT NULL AND h.hour_start < c.cutoff
        """,
        {
            "ids": json.dumps(ids),
            "profiles": json.dumps([(*key, at) for key, at in profiles.items()]),
        },
    ):
        a, b = row["from_node_id"], row["to_node_id"]
        n = row["node"]
        other = b if n == a else a
        refs[n, row["link_kind"], row["channel"]].append(
            (other, row["hour_start"], row["last_seen_at"], row["km"])
        )
    moves: dict[int, list[int]] = defaultdict(list)
    previous: dict[int, tuple[float, float]] = {}
    for n, at, lat, lon in connection.execute(
        "SELECT node_id, at, latitude, longitude FROM node_position "
        "WHERE node_id IN (SELECT value FROM json_each(?)) ORDER BY node_id, at",
        (json.dumps(ids),),
    ):
        if n in previous and great_circle_km(*previous[n], lat, lon) > MOVED_KM:
            moves[n].append(at)
        previous[n] = (lat, lon)
    result = {}
    for pair, row in targets.items():
        ends = []
        for n, other in (pair, pair[::-1]):
            epoch = bisect.bisect_right(moves[n], row["last_seen_at"])
            since = moves[n][epoch - 1] if epoch else 0
            partners: dict[int, list[float]] = defaultdict(list)
            occupied = set()
            last_seen = None
            for peer, hour, last, km in refs[n, row["link_kind"], row["channel"]]:
                if peer != other and since <= last and hour < row["hour_start"]:
                    partners[peer].append(km)
                    occupied.add(hour)
                    last_seen = max(last_seen or 0, last)
            supported = not (
                len(partners) < REACH_PARTNERS
                or len(occupied) < REACH_HOURS
                or len({hour // DAY_MS for hour in occupied}) < REACH_DAYS
                or row["hour_start"] - min(occupied) < DAY_MS
            )
            xs = sorted(statistics.median(kms) for kms in partners.values())
            median = statistics.median(xs) if xs else None
            iqr = None
            if xs:
                q25, _, q75 = (
                    statistics.quantiles(xs, n=4, method="inclusive")
                    if len(xs) > 1
                    else (xs[0], xs[0], xs[0])
                )
                iqr = q75 - q25
            threshold = (
                REACH_REVIEW_KM + REACH_FACTOR * (median + iqr)
                if supported and median is not None and iqr is not None
                else None
            )
            ends.append(
                ReachProfile(
                    node_id=n,
                    partners=len(partners),
                    hours=len(occupied),
                    days=len({hour // DAY_MS for hour in occupied}),
                    first_hour_at=from_epoch_ms(min(occupied)) if occupied else None,
                    last_seen_at=optional_from_epoch_ms(last_seen),
                    median_km=median,
                    iqr_km=iqr,
                    threshold_km=threshold,
                )
            )
        result[pair] = ReachComparison(
            link_kind=row["link_kind"],
            channel=row["channel"],
            before_at=from_epoch_ms(row["hour_start"]),
            ends=(ends[0], ends[1]),
        )
    return result


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
    shared = shared_nodes(connection)
    partners = direct_partners(connection, estimate_since_ms(connection))
    ends = {}
    for node_id in ids:
        row = current.get(node_id)
        assessment = partners.get(node_id)
        found = sorted(assessment.counted.values()) if assessment else []
        median = statistics.median(found) if found else None
        limit = doubt_limit_km(None if row is None else row["altitude_meters"])
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
            shared=node_id in shared,
            direct_partners=len(found),
            blamed_partners=assessment.blamed if assessment else 0,
            traceroute_corroborated=assessment.corroborated if assessment else False,
            nearest_partner_km=found[0] if found else None,
            median_partner_km=None if median is None else round(median, 1),
            doubt_limit_km=round(limit),
            over_limit=median is not None and median > limit,
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
    links_page_rows: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        links_page_rows[row["link_kind"], row["channel"]].append(row)
    shown_row = links_page_rows[longest["link_kind"], longest["channel"]]
    newest = max(shown_row, key=lambda row: row["last_seen_at"])
    long_hours = [row for row in measured if row["km"] >= LONG_LINK_KM]
    longest_km = longest["km"]
    unlikely = terrain is not None and terrain.unlikely
    badges = {
        key: _links_page_doubts(a, b, key_rows, unlikely)
        for key, key_rows in links_page_rows.items()
    }
    evidence = tuple(
        _evidence(
            kind,
            kind_rows,
            a,
            b,
            unlikely,
            tuple(
                doubt
                for doubt in get_args(Doubt)
                if any(doubt in badges[key] for key in badges if key[0] == kind)
            ),
        )
        for kind in LINK_KINDS
        if (kind_rows := [row for row in rows if row["link_kind"] == kind])
    )
    drawn_km = distance_km(a.node, b.node)
    drawn = any(kind.map_hidden_because is None for kind in evidence)
    doubts = badges[longest["link_kind"], longest["channel"]]
    doubt = _doubt(a, b, doubts, unlikely_pairs)
    unconfirmed = "unconfirmed" in doubts

    def still_long(km: float | None) -> bool:
        return km is not None and abs(longest_km - km) <= MOVED_KM

    shown: Literal["long", "shorter", "hidden"] = "shorter"
    if (drawn and still_long(drawn_km)) or (
        doubt is None and not unconfirmed and still_long(newest["km"])
    ):
        shown = "long"
    elif not drawn and (doubt is not None or unconfirmed):
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
        unconfirmed=unconfirmed,
        now_km=reported_km(a.node, b.node),
        drawn_km=drawn_km,
        shown=shown,
        doubt=doubt,
        channels=tuple(sorted({str(row["channel"]) for row in rows})),
        evidence=evidence,
        terrain=terrain if longest_km >= LONG_LINK_KM else None,
        checks=(),
        reach=None,
    )
    return LongLink(**{**vars(link), "checks": _checks(link)})


def _links_page_doubts(
    a: LongLinkEnd, b: LongLinkEnd, rows: Sequence[sqlite3.Row], unlikely: bool
) -> tuple[Doubt, ...]:
    """The badges of the Links page's row of these hours, one kind and channel,
    over all history (links.link_table)."""
    newest = max(rows, key=lambda row: row["last_seen_at"])
    return link_doubts(
        a.doubtful or b.doubtful,
        newest["km"],
        unlikely,
        any(row["confirmed"] for row in rows),
        bool(newest["suspect_distance"]),
        a.shared or b.shared,
    )


def _doubt(
    a: LongLinkEnd, b: LongLinkEnd, doubts: Sequence[Doubt], unlikely_pairs: Mapping[int, int]
) -> LongLinkDoubt | None:
    ends = (a, b)
    if "shared" in doubts:
        return LongLinkDoubt("shared", tuple(end.node.node_id for end in ends if end.shared))
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
    doubts: tuple[Doubt, ...],
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
        doubts=doubts,
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
                reasons.append("the signal is unusually strong for the distance")
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
        if end.shared:
            return f"{_name(end)} is a shared node number"
    for end in (a, b):
        if end.doubtful:
            return f"{_name(end)} has a doubtful position"
    last_seen = from_epoch_ms(last_seen_ms)
    for end in (a, b):
        if end.moved_at is not None and last_seen <= end.moved_at:
            return f"it was heard before {_name(end)} last moved"
    return "it was heard before an end last moved"


def _checks(link: LongLink) -> tuple[ReviewCheck, ...]:
    checks = []
    if link.longest_km - link.shortest_km > MOVED_KM:
        checks.append(ReviewCheck("distance_range"))
    kinds = {evidence.link_kind: evidence for evidence in link.evidence}
    if sum(evidence.a_to_b + evidence.b_to_a for evidence in link.evidence) == 1:
        checks.append(ReviewCheck("heard_once"))
    if "direct" not in kinds and "neighbor" not in kinds:
        checks.append(ReviewCheck("traceroute_only"))
    if "traceroute" in kinds and kinds["traceroute"].zero_snr:
        checks.append(ReviewCheck("mqtt_hop"))
    for end in (link.node_a, link.node_b):
        node_id = end.node.node_id
        if (
            not end.traceroute_corroborated
            and end.direct_partners >= 2
            and (end.nearest_partner_km or 0) > LOCAL_PARTNER_KM
        ):
            checks.append(ReviewCheck("far_partners", node_id))
        if end.position_from_map_report:
            checks.append(ReviewCheck("map_report", node_id))
        if (
            end.moved_at is not None
            and end.moved_at <= link.longest_at
            and end.position_observed_at is not None
            and end.position_observed_at <= link.longest_at
            and (link.longest_at - end.position_observed_at).total_seconds()
            > STALE_POSITION_HOURS * 3600
        ):
            checks.append(ReviewCheck("stale_position", node_id))
    if link.terrain is None and link.longest_km >= LONG_LINK_KM:
        checks.append(ReviewCheck("terrain_unchecked"))
    return tuple(checks)


def _name(end: LongLinkEnd) -> str:
    node = end.node
    return node.long_name or node.short_name or f"!{node.node_id:08x}"
