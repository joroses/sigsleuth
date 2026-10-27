"""Coverage from signal surveys: saving a phone's uploaded measurements,
deciding which node each one heard, and the map's coverage cells.

A phone running the Meshtastic Android fork's signal survey, connected to its
own node (the survey node), records every LoRa packet that node hears with
where the phone was, and uploads them in batches of one session and up to
MAX_UPLOAD_MEASUREMENTS measurements (POST /api/v1/coverage/uploads).

- Retries are harmless: a (session, seq) already saved is ignored, and the
  reply's last_seq tells the phone where to continue.
- Unusable rows are skipped and counted by reason, never fatal: no position,
  a fix worse than MAX_ACCURACY_M or older than MAX_FIX_AGE_MS, or a time
  outside the age limit or more than MAX_FUTURE_MS ahead.
- Everything is read before the write transaction opens, so it holds the
  write lock, which capture also needs, only to insert, to save its senders'
  OK to MQTT bits (reported.save_bit) and to refresh their opt-outs
  (opt_out.refresh_opt_outs), a few seeks each.
- Each measurement is saved with its cell, its SNR margin and the node it
  heard (heard_node), decided once, at upload (schema.sql says why). A survey
  link (refresh_survey_links), from survey evidence alone, can name a relay
  later, and wins over upload's name.
- The reply names each usable measurement's heard node as the map reads it,
  for the phone's relay list; a resent batch reads what was saved.

The map counts a measurement unless it is proven stale, and only while its
node is known alive or the measurement is recent (_ALIVE_SQL;
docs/coverage.md).
"""

import json
import math
import sqlite3
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sigsleuth import relays
from sigsleuth.db import (
    NodeSummary,
    from_epoch_ms,
    nodes_by_id,
    now_ms,
    optional_from_epoch_ms,
    to_epoch_ms,
    transaction,
)
from sigsleuth.decode import known_hop_start
from sigsleuth.derive import HOUR_MS, SavedHeader, is_direct_reception
from sigsleuth.opt_out import SHOWN_PACKET, opted_out, opted_out_sql, refresh_opt_outs
from sigsleuth.positions import own_reach
from sigsleuth.radio import PRESET_SPREADING_FACTORS, fade_margin, great_circle_km, preset_name
from sigsleuth.relays import (
    DAY_MS,
    RULE_OUT_KM,
    Relayed,
    RelayEvidence,
    candidates_left,
    cannot_relay_at,
    direct_signals,
    name_relay,
    node_km,
    node_radios,
    nodes_by_byte,
)
from sigsleuth.reported import save_bit

MAX_UPLOAD_MEASUREMENTS = 1_000
MAX_ACCURACY_M = 100
MAX_FIX_AGE_MS = 10_000
MAX_FUTURE_MS = 60 * 60 * 1_000
# An MQTT report of the same sender and packet ID within this long of the
# phone hearing it is the same transmission.
MQTT_MATCH_MS = HOUR_MS
# Survey links (refresh_survey_links): a node some survey node heard directly
# within SURVEY_LINK_KM of the phone, in the first of these time tiers that
# has any, like the phone's own relay guess (DirectNeighbours in the Android
# fork), which reads only its own receptions and looks back 30 days. Pooled
# survey nodes rarely cover the same weeks, so 60 days lets one name the
# relays of another (2026-10-17). Not measured against the truth yet:
# scripts/coverage-backtest.py compares other bounds.
SURVEY_LINK_KM = 30
SURVEY_LINK_TIERS_MS = (HOUR_MS, 60 * DAY_MS)
# A relayed copy cannot arrive much stronger than its relay's own packets
# heard from the same place (too_strong). Measured with gateways standing in
# for phones on live loranet.pl, 2026-10 (docs/relay-evidence.md): 30 dB rules
# out 0.035% of true relays and 15% of the wrong candidates heard directly.
# Untested at ground level.
TOO_STRONG_DB = 30
TOO_STRONG_KM = 1
# The survey app's relay node list (relay_nodes). Its reach reads took 1 s
# for 3,000 placed nodes on a laptop's two weeks of loranet.pl (2026-10-26);
# the app asks about once a day.
MAX_RELAY_NODES = 10_000
# Fine coverage cells are Web Mercator tiles at this zoom: about 305 m times
# cos(latitude) across, so 227 m at 42°N.
FINE_LEVEL = 17
MAX_MERCATOR_LATITUDE = 85.05112878

# How upload decided the heard node (heard_node), as saved. Uploads no longer
# guess nearest; the map reads saved nearest rows as unknown.
SavedHow = Literal[
    "direct", "reported", "gateway_link", "sender_link", "nearest", "only_candidate", "unknown"
]
# How the map names the heard node: as saved, or survey_link (coverage_relay).
HeardHow = Literal[
    "direct", "reported", "gateway_link", "sender_link", "survey_link", "only_candidate", "unknown"
]
# The guesses, drawn dashed: direct and survey_link come from a direct reception.
GUESSED: tuple[HeardHow, ...] = ("reported", "gateway_link", "sender_link", "only_candidate")

# A radio network (Network) and a relay byte. Not the survey node: a relay
# byte is the relay's, whichever survey node heard it.
LinkKey = tuple[int | None, float | None, float | None, int]
# A survey direct reception: when, where, by which survey node, its signal.
Reception = tuple[int, float, float, int, float | None]
# Per LinkKey, each node's direct receptions, in time order.
DirectReceptions = dict[LinkKey, dict[int, list[Reception]]]


class UploadSession(BaseModel):
    """The phone's session and the survey node's radio settings."""

    id: str = Field(min_length=1, max_length=64)
    receiver_node_id: int = Field(ge=1, le=0xFFFFFFFF)
    receiver_name: str | None = Field(default=None, max_length=256)
    modem_preset: str | None = Field(default=None, max_length=64)
    spreading_factor: int | None = Field(default=None, ge=5, le=12)
    bandwidth_khz: float | None = None
    frequency_mhz: float | None = None
    app_version: str | None = Field(default=None, max_length=64)


class UploadMeasurement(BaseModel):
    """One packet the survey node heard. Times are epoch milliseconds on the
    phone's clock. A missing SNR or RSSI is null; 0 is a real reading."""

    seq: int = Field(ge=0)
    heard_at_ms: int
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    fix_age_ms: int | None = None
    sender_node_id: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    recipient_node_id: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    packet_id: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    rx_snr: float | None = None
    rx_rssi: int | None = None
    hop_start: int | None = Field(default=None, ge=0, le=7)
    hop_limit: int | None = Field(default=None, ge=0, le=7)
    relay_byte: int | None = Field(default=None, ge=0, le=255)
    ok_to_mqtt: bool | None = None
    default_key: bool | None = None


class Upload(BaseModel):
    session: UploadSession
    measurements: list[UploadMeasurement] = Field(max_length=MAX_UPLOAD_MEASUREMENTS)


@dataclass(frozen=True)
class CoverageCandidate:
    """A possible relay, km from a measurement or a cell's centre."""

    node_id: int
    long_name: str | None
    short_name: str | None
    km: float


@dataclass(frozen=True)
class UploadHeard:
    """The heard node as the map reads it (_SHOWN), or possible relay hints."""

    seq: int
    node_id: int | None
    how: HeardHow
    candidates: tuple[CoverageCandidate, ...] = ()


@dataclass(frozen=True)
class UploadNode:
    node_id: int
    long_name: str | None
    short_name: str | None


@dataclass(frozen=True)
class UploadResult:
    """saved counts new measurements, already_saved those the server held
    from an earlier upload. skipped counts unusable ones by reason. last_seq
    is the highest seq in the batch, saved or not, so the phone continues
    after it; None for an empty batch. heard has each usable measurement's
    heard node and possible relays when unknown, read after the survey links
    are refreshed, and nodes the names of those named."""

    saved: int
    already_saved: int
    skipped: dict[str, int] = field(default_factory=dict)
    last_seq: int | None = None
    heard: list[UploadHeard] = field(default_factory=list)
    nodes: list[UploadNode] = field(default_factory=list)


def skip_reason(measurement: UploadMeasurement, *, now: int, max_age_days: int) -> str | None:
    """Why a measurement cannot be used, or None when it can. max_age_days
    is how long measurements are kept (0 for no limit): a phone that was
    offline may upload weeks late."""
    m = measurement
    if (
        m.latitude is None
        or m.longitude is None
        or not (-90 <= m.latitude <= 90 and -180 <= m.longitude <= 180)
        or (m.latitude == 0 and m.longitude == 0)
    ):
        return "no_position"
    if m.accuracy_m is not None and m.accuracy_m > MAX_ACCURACY_M:
        return "inaccurate"
    if m.fix_age_ms is not None and m.fix_age_ms > MAX_FIX_AGE_MS:
        return "old_fix"
    if max_age_days and m.heard_at_ms < now - max_age_days * DAY_MS:
        return "too_old"
    if m.heard_at_ms > now + MAX_FUTURE_MS:
        return "in_future"
    if not m.sender_node_id:
        return "no_sender"
    return None


def save_upload(
    connection: sqlite3.Connection, upload: Upload, *, max_age_days: int
) -> UploadResult:
    """Save an upload's usable measurements, with their cells and heard
    nodes, and their session."""
    now = now_ms()
    skipped: Counter[str] = Counter()
    session = upload.session
    rows = []
    usable = []
    bits = []
    for m in upload.measurements:
        reason = skip_reason(m, now=now, max_age_days=max_age_days)
        if reason is not None:
            skipped[reason] += 1
            continue
        usable.append(m)
        if m.ok_to_mqtt is not None and m.sender_node_id:
            bits.append((m.sender_node_id, m.ok_to_mqtt, m.heard_at_ms))
        # As capture reads MQTT headers: an unknown hop start leaves both
        # hop fields unknown.
        hop_start = None
        if m.hop_start is not None and m.sender_node_id is not None:
            hop_start = known_hop_start(m.hop_start, m.hop_limit, m.relay_byte, m.sender_node_id)
        rows.append(
            {
                "seq": m.seq,
                "heard_at": m.heard_at_ms,
                "latitude": m.latitude,
                "longitude": m.longitude,
                "accuracy_m": m.accuracy_m,
                "fix_age_ms": m.fix_age_ms,
                "sender_node_id": m.sender_node_id,
                "recipient_node_id": m.recipient_node_id,
                "packet_id": m.packet_id or None,
                "rx_snr": m.rx_snr,
                "rx_rssi": m.rx_rssi,
                "hop_start": hop_start,
                "hop_limit": None if hop_start is None else m.hop_limit,
                "relay_byte": m.relay_byte,
                "ok_to_mqtt": m.ok_to_mqtt,
                "default_key": m.default_key,
                "receiver_node_id": session.receiver_node_id,
                "spreading_factor": session.spreading_factor,
                "bandwidth_khz": session.bandwidth_khz,
                "frequency_mhz": session.frequency_mhz,
            }
        )
    points = coverage_points(connection, rows)
    saved = 0
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO coverage_session (
                uuid, receiver_node_id, receiver_name, modem_preset, spreading_factor,
                bandwidth_khz, frequency_mhz, app_version, first_uploaded_at, last_uploaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?9, ?9)
            ON CONFLICT (uuid) DO UPDATE SET
                receiver_node_id = excluded.receiver_node_id,
                receiver_name = excluded.receiver_name,
                modem_preset = excluded.modem_preset,
                spreading_factor = excluded.spreading_factor,
                bandwidth_khz = excluded.bandwidth_khz,
                frequency_mhz = excluded.frequency_mhz,
                app_version = excluded.app_version,
                last_uploaded_at = max(last_uploaded_at, excluded.last_uploaded_at)
            """,
            (
                session.id,
                session.receiver_node_id,
                session.receiver_name,
                session.modem_preset,
                session.spreading_factor,
                session.bandwidth_khz,
                session.frequency_mhz,
                session.app_version,
                now,
            ),
        )
        session_row_id = connection.execute(
            "SELECT id FROM coverage_session WHERE uuid = ?", (session.id,)
        ).fetchone()[0]
        for row, point in zip(rows, points, strict=True):
            saved += connection.execute(
                """
                INSERT OR IGNORE INTO coverage_measurement (
                    session_row_id, seq, heard_at, latitude, longitude, accuracy_m, fix_age_ms,
                    sender_node_id, recipient_node_id, packet_id,
                    rx_snr, rx_rssi, hop_start, hop_limit, relay_byte, ok_to_mqtt, default_key,
                    cell_x, cell_y, heard_node_id, heard_how, candidates, margin_db
                ) VALUES (
                    :session_row_id, :seq, :heard_at, :latitude, :longitude, :accuracy_m,
                    :fix_age_ms, :sender_node_id, :recipient_node_id, :packet_id,
                    :rx_snr, :rx_rssi, :hop_start, :hop_limit, :relay_byte, :ok_to_mqtt,
                    :default_key,
                    :cell_x, :cell_y, :heard_node_id, :heard_how, :candidates, :margin_db
                )
                """,
                {
                    **row,
                    "session_row_id": session_row_id,
                    "cell_x": point.cell_x,
                    "cell_y": point.cell_y,
                    "heard_node_id": point.heard.node_id,
                    "heard_how": point.heard.how,
                    "candidates": point.heard.candidates,
                    "margin_db": point.margin_db,
                },
            ).rowcount
        for sender, bit, heard_at in bits:
            save_bit(connection, sender, bit, heard_at)
        refresh_opt_outs(connection, {sender for sender, _, _ in bits})
    # Already saved rows too: when a refresh fails after the insert commits,
    # the phone gets a 503 and resends the batch, which saves nothing.
    if points:
        times = [point.heard_at for point in points]
        reach = SURVEY_LINK_TIERS_MS[-1]
        refresh_survey_links(
            connection, session_row_id, start=min(times) - reach, end=max(times) + reach
        )
    seqs = [m.seq for m in upload.measurements]
    heard = saved_heard(connection, session_row_id, [m.seq for m in usable])
    named = nodes_by_id(connection, {h.node_id for h in heard if h.node_id is not None})
    return UploadResult(
        saved=saved,
        already_saved=len(rows) - saved,
        skipped=dict(skipped),
        last_seq=max(seqs) if seqs else None,
        heard=heard,
        nodes=[UploadNode(n.node_id, n.long_name, n.short_name) for n in named.values()],
    )


def saved_heard(
    connection: sqlite3.Connection, session_row_id: int, seqs: Sequence[int]
) -> list[UploadHeard]:
    """The heard node of a session's saved measurements with these seqs."""
    if not seqs:
        return []
    rows = connection.execute(
        f"""
        SELECT m.seq, {_SHOWN} AS node_id, {_SHOWN_HOW} AS how,
               m.relay_byte, m.latitude, m.longitude, s.spreading_factor,
               m.sender_node_id, s.receiver_node_id
        FROM {_MEASUREMENTS}
        JOIN coverage_session AS s ON s.id = m.session_row_id
        WHERE m.session_row_id = ? AND m.seq IN (SELECT value FROM json_each(?))
        ORDER BY m.seq
        """,
        (session_row_id, json.dumps(sorted(set(seqs)))),
    ).fetchall()
    unknown = [row for row in rows if row["node_id"] is None and row["relay_byte"]]
    candidates = dict(
        zip(
            (row["seq"] for row in unknown),
            relay_candidates(
                connection,
                [
                    (
                        row["relay_byte"],
                        row["latitude"],
                        row["longitude"],
                        {row["spreading_factor"]} if row["spreading_factor"] else set(),
                        {row["sender_node_id"], row["receiver_node_id"]},
                    )
                    for row in unknown
                ],
            ),
            strict=True,
        )
    )
    return [
        UploadHeard(int(row["seq"]), row["node_id"], row["how"], candidates.get(row["seq"], ()))
        for row in rows
    ]


@dataclass(frozen=True)
class HeardNode:
    """Which node's transmission the survey node received, and how upload
    decided that (SavedHow). node_id is None when unknown. candidates counts
    the candidates the rule-outs left (heard_node); 1 when heard directly."""

    node_id: int | None
    how: SavedHow
    candidates: int


@dataclass(frozen=True)
class CoveragePoint:
    """What an upload decides about a measurement (schema.sql)."""

    cell_x: int
    cell_y: int
    heard_at: int
    sender_node_id: int
    heard: HeardNode
    margin_db: float | None


def coverage_points(
    connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
) -> list[CoveragePoint]:
    """The coverage points of measurement rows, each with its session's
    receiver_node_id and radio settings (spreading_factor, bandwidth_khz,
    frequency_mhz)."""
    if not rows:
        return []
    evidence = relay_evidence(connection, rows)
    direct = _survey_direct(connection, rows, evidence)
    return [_point(row, heard_node(connection, evidence, direct, row)) for row in rows]


def _survey_direct(
    connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], evidence: RelayEvidence
) -> DirectReceptions:
    """The rows' survey nodes' direct receptions of the nodes sending their
    relay bytes, saved or among the rows, within SURVEY_LINK_TIERS_MS[-1] of
    them, for too_strong."""
    heard = [int(row["heard_at"]) for row in rows]
    reach = SURVEY_LINK_TIERS_MS[-1]
    saved = connection.execute(
        f"""
        SELECT {SURVEY_LINK_COLUMNS}
        FROM coverage_measurement AS m INDEXED BY coverage_measurement_sender
        JOIN coverage_session AS s ON s.id = m.session_row_id
        WHERE m.sender_node_id IN (SELECT value FROM json_each(:nodes))
          AND m.heard_at BETWEEN :start AND :end
          AND m.heard_how = 'direct' AND m.rx_rssi != 0
          AND s.receiver_node_id IN (SELECT value FROM json_each(:receivers))
        """,
        {
            "nodes": json.dumps(sorted({n for nodes in evidence.by_byte.values() for n in nodes})),
            "start": min(heard) - reach,
            "end": max(heard) + reach,
            "receivers": json.dumps(sorted({int(row["receiver_node_id"]) for row in rows})),
        },
    ).fetchall()
    own = [{**row, "heard_how": "direct"} for row in rows if _is_direct(row)]
    return heard_directly([*saved, *own])


def relay_evidence(
    connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
) -> RelayEvidence:
    """The RelayEvidence for naming the relays of measurement rows."""
    return relays.relay_evidence(
        connection,
        {row["relay_byte"] for row in rows if row["relay_byte"]},
        {int(row["sender_node_id"]) for row in rows},
        [int(row["heard_at"]) for row in rows],
        spreading_factors={row["spreading_factor"] for row in rows if row["spreading_factor"]},
    )


def _point(row: Mapping[str, Any], heard: HeardNode) -> CoveragePoint:
    """A measurement row's coverage point, with the node it heard."""
    cell_x, cell_y = fine_cell(row["latitude"], row["longitude"])
    snr, spreading_factor = row["rx_snr"], row["spreading_factor"]
    return CoveragePoint(
        cell_x=cell_x,
        cell_y=cell_y,
        heard_at=int(row["heard_at"]),
        sender_node_id=int(row["sender_node_id"]),
        heard=heard,
        margin_db=None
        if snr is None or spreading_factor is None
        else round(fade_margin(snr, spreading_factor), 2),
    )


def fine_cell(latitude: float, longitude: float) -> tuple[int, int]:
    """The fine coverage cell holding a position: its Web Mercator tile at
    zoom FINE_LEVEL, as (x, y)."""
    return tile(latitude, longitude, FINE_LEVEL)


def tile(latitude: float, longitude: float, level: int) -> tuple[int, int]:
    """The Web Mercator tile at a zoom level that holds a position."""
    scale = 1 << level
    latitude = max(min(latitude, MAX_MERCATOR_LATITUDE), -MAX_MERCATOR_LATITUDE)
    phi = math.radians(latitude)
    x = int((longitude + 180) / 360 * scale)
    y = int((1 - math.log(math.tan(phi) + 1 / math.cos(phi)) / math.pi) / 2 * scale)
    return min(max(x, 0), scale - 1), min(max(y, 0), scale - 1)


def heard_node(
    connection: sqlite3.Connection,
    evidence: RelayEvidence,
    direct: DirectReceptions,
    row: Mapping[str, Any],
) -> HeardNode:
    """The node a measurement heard:

    1. direct: the survey node heard the sender itself, by the map's rule for
       direct receptions (is_direct_reception).
    2. A relay, by relays.name_relay among the candidates_left that are not
       too_strong by the survey node's direct receptions, with the MQTT
       gateways' reports of the same packet (_mqtt_reports).
    3. unknown: no relay byte (older firmware), or nothing names one.

    A survey link (refresh_survey_links) wins over all but direct when the
    map reads it.
    """
    sender, receiver = int(row["sender_node_id"]), int(row["receiver_node_id"])
    if _is_direct(row):
        return HeardNode(sender, "direct", 1)
    if not row["relay_byte"]:
        return HeardNode(None, "unknown", 0)
    relayed = Relayed(
        sender=sender,
        receiver=receiver,
        at=int(row["heard_at"]),
        position=(row["latitude"], row["longitude"]),
        spreading_factor=row["spreading_factor"],
        hop_start=row["hop_start"],
        hop_limit=row["hop_limit"],
        relay_byte=int(row["relay_byte"]),
        reports=[]
        if row["hop_limit"] is None
        else [
            (
                int(report["gateway_node_id"]),
                report["hop_limit"],
                report["relay_byte"],
                report["rx_snr"],
            )
            for report in _mqtt_reports(connection, row)
        ],
    )
    receptions = direct.get(_link_key(row), {})
    left = [
        node
        for node in candidates_left(evidence, relayed)
        if not too_strong(receptions.get(node, []), row)
    ]
    if not left:
        return HeardNode(None, "unknown", 0)
    signals = direct_signals(
        connection,
        ((node, gateway) for node in left for gateway in relayed.cohearers()),
        relayed.at,
    )
    named = name_relay(evidence, relayed, left, signals)
    if named is None:
        return HeardNode(None, "unknown", len(left))
    return HeardNode(named[0], named[1], len(left))


def _is_direct(row: Mapping[str, Any]) -> bool:
    header = SavedHeader(
        row["rx_snr"], row["rx_rssi"], row["hop_start"], row["hop_limit"], row["relay_byte"]
    )
    return is_direct_reception(int(row["sender_node_id"]), int(row["receiver_node_id"]), header)


def _mqtt_reports(connection: sqlite3.Connection, row: Mapping[str, Any]) -> list[sqlite3.Row]:
    """MQTT gateways' reports of the measurement's packet: the same sender and
    packet ID, saved within MQTT_MATCH_MS of when the phone heard it."""
    if row["packet_id"] is None:
        return []
    return connection.execute(
        """
        SELECT r.gateway_node_id, r.hop_limit, r.relay_byte, r.rx_snr
        FROM packet AS t INDEXED BY packet_identity_time
        JOIN reception AS r ON r.packet_row_id = t.id
        WHERE t.sender_node_id = :sender AND t.packet_id = :packet
          AND t.last_received_at >= :heard_at - :window
          AND t.first_received_at <= :heard_at + :window
        ORDER BY r.id
        """,
        {
            "sender": row["sender_node_id"],
            "packet": row["packet_id"],
            "heard_at": row["heard_at"],
            "window": MQTT_MATCH_MS,
        },
    ).fetchall()


def refresh_survey_links(
    connection: sqlite3.Connection,
    session_row_id: int | None = None,
    *,
    start: int = 0,
    end: int = 2**62,
) -> None:
    """Refill the survey links (coverage_relay) of the relayed measurements
    heard from start to end (epoch ms): their one survey_candidates node.
    With session_row_id, only the sessions with its radio settings; else
    every session (rederive).

    Links read saved measurements alone, so each call re-checks the
    measurements in its window against all their evidence: the order uploads
    arrive in makes no difference."""
    reach = SURVEY_LINK_TIERS_MS[-1]
    sessions = None
    if session_row_id is not None:
        sessions = [
            int(row[0])
            for row in connection.execute(
                """
                SELECT o.id FROM coverage_session AS s JOIN coverage_session AS o
                  ON o.spreading_factor IS s.spreading_factor
                 AND o.bandwidth_khz IS s.bandwidth_khz
                 AND o.frequency_mhz IS s.frequency_mhz
                WHERE s.id = ?
                """,
                (session_row_id,),
            )
        ]
    in_sessions = "AND m.session_row_id IN (SELECT value FROM json_each(:sessions))"
    rows = connection.execute(
        f"""
        SELECT {SURVEY_LINK_COLUMNS}, l.node_id AS linked
        FROM coverage_measurement AS m JOIN coverage_session AS s ON s.id = m.session_row_id
        LEFT JOIN coverage_relay AS l ON l.measurement_id = m.id
        WHERE m.heard_at BETWEEN :start AND :end
          AND (m.heard_how = 'direct' OR m.relay_byte IS NOT NULL)
          {"" if sessions is None else in_sessions}
        """,
        {"start": start - reach, "end": end + reach, "sessions": json.dumps(sessions)},
    ).fetchall()
    heard = heard_directly(rows)
    targets = [
        row for row in rows if row["heard_how"] != "direct" and start <= row["heard_at"] <= end
    ]
    # Only changed links are written: a phone that records all day re-checks
    # thousands of measurements per upload, and capture waits on the write.
    changed = []
    for row in targets:
        found = survey_candidates(heard, row)
        link = found[0] if len(found) == 1 else None
        if link != row["linked"]:
            changed.append((int(row["id"]), link))
    if not changed:
        return
    with transaction(connection):
        connection.executemany(
            "DELETE FROM coverage_relay WHERE measurement_id = ?",
            [(id_,) for id_, link in changed if link is None],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO coverage_relay (measurement_id, node_id) VALUES (?, ?)",
            [(id_, link) for id_, link in changed if link is not None],
        )


# The measurement and session columns survey links read (m and s).
SURVEY_LINK_COLUMNS = """
    m.id, m.heard_at, m.latitude, m.longitude, m.sender_node_id, m.relay_byte, m.heard_how,
    m.rx_rssi, m.rx_snr, s.receiver_node_id, s.spreading_factor, s.bandwidth_khz, s.frequency_mhz
"""


def heard_directly(rows: Iterable[Mapping[str, Any]]) -> DirectReceptions:
    """The direct receptions among rows (SURVEY_LINK_COLUMNS)."""
    heard: DirectReceptions = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row["heard_how"] == "direct":
            heard[_link_key(row)][int(row["sender_node_id"])].append(
                (
                    int(row["heard_at"]),
                    row["latitude"],
                    row["longitude"],
                    int(row["receiver_node_id"]),
                    signal_db(row["rx_rssi"], row["rx_snr"]),
                )
            )
    for nodes in heard.values():
        for receptions in nodes.values():
            receptions.sort(key=lambda reception: reception[0])
    return heard


def survey_candidates(
    heard: DirectReceptions,
    row: Mapping[str, Any],
    *,
    km: float = SURVEY_LINK_KM,
    tiers_ms: Sequence[int] = SURVEY_LINK_TIERS_MS,
) -> list[int]:
    """The nodes that may have relayed a measurement row's packet, by survey
    evidence: those any survey node heard directly, on the same radio
    settings, sending the same relay byte within km of the phone, in the
    first tier (ms either side) that has any. A survey link when exactly one.
    A node never relays its own packet, a survey node never records its own
    rebroadcast, and a too_strong node is left out."""
    nodes = heard.get(_link_key(row), {})
    excluded = (row["sender_node_id"], row["receiver_node_id"])
    for tier in tiers_ms:
        found = [
            node
            for node, receptions in nodes.items()
            if node not in excluded
            and _heard_near(receptions, row, tier, km)
            and not too_strong(receptions, row)
        ]
        if found:
            return found
    return []


def signal_db(rssi: float | None, snr: float | None) -> float | None:
    """A reception's signal in dB: its RSSI, plus its SNR when below 0, as
    RSSI then reads the noise. None without an RSSI (0 means missing)."""
    if not rssi:
        return None
    return rssi + min(snr, 0) if snr is not None else rssi


def too_strong(receptions: list[Reception], row: Mapping[str, Any]) -> bool:
    """Whether a measurement row cannot have come from a node by its direct
    receptions (time-sorted): the row's signal is over TOO_STRONG_DB above
    every signal at which the row's own survey node heard the node directly
    within TOO_STRONG_KM, SURVEY_LINK_TIERS_MS[-1] either side. Weaker is
    normal (a pocket, indoors), and other survey nodes' radios read RSSI
    differently. False without such a reading, so it only ever rules out."""
    signal = signal_db(row["rx_rssi"], row["rx_snr"])
    if signal is None:
        return False
    at, receiver = int(row["heard_at"]), int(row["receiver_node_id"])
    reach = SURVEY_LINK_TIERS_MS[-1]
    first = bisect_left(receptions, at - reach, key=lambda reception: reception[0])
    checked = False
    for index in range(first, len(receptions)):
        heard_at, latitude, longitude, heard_by, heard_signal = receptions[index]
        if heard_at > at + reach:
            break
        if heard_by != receiver or heard_signal is None:
            continue
        if great_circle_km(row["latitude"], row["longitude"], latitude, longitude) > TOO_STRONG_KM:
            continue
        if signal <= heard_signal + TOO_STRONG_DB:
            return False
        checked = True
    return checked


def _link_key(row: Mapping[str, Any]) -> LinkKey:
    return (
        row["spreading_factor"],
        row["bandwidth_khz"],
        row["frequency_mhz"],
        int(row["relay_byte"]),
    )


def _heard_near(
    receptions: list[Reception], row: Mapping[str, Any], within_ms: int, km: float
) -> bool:
    """Whether any of a node's direct receptions (time-sorted) was within
    within_ms and km of the measurement row."""
    at = int(row["heard_at"])
    first = bisect_left(receptions, at - within_ms, key=lambda reception: reception[0])
    # By index: a slice would copy every later reception for each measurement.
    for index in range(first, len(receptions)):
        heard_at, latitude, longitude, _, _ = receptions[index]
        if heard_at > at + within_ms:
            return False
        if great_circle_km(row["latitude"], row["longitude"], latitude, longitude) <= km:
            return True
    return False


# The map asks for cells at its zoom + CELL_ZOOM_OFFSET, between these levels.
# MapLibre draws the world 512 px wide at zoom 0, so a cell is 8 to 16 px wide
# on screen until the fine level, when it grows with the zoom.
CELL_ZOOM_OFFSET = 6
MIN_CELL_LEVEL = 8
MAX_CELLS = 5_000
CELL_NEWEST = 20
CELL_MAX_NODES = 50
CELL_CANDIDATES = 3
# A cell lists candidates for its commonest unknown relays only: each byte
# costs about 3 ms on a live copy (2026-10-18).
CELL_CANDIDATE_RELAYS = 10
EARTH_CIRCUMFERENCE_M = 40_075_016.686

# Measurements as the map reads them, aliased m: the heard node (_HEARD) and
# how it was named (_HOW) are a survey link's where there is one, else
# upload's. Saved nearest guesses read as unknown (heard_node says why).
_MEASUREMENTS = "coverage_measurement AS m LEFT JOIN coverage_relay AS l ON l.measurement_id = m.id"
_HEARD = (
    "CASE WHEN l.node_id IS NOT NULL THEN l.node_id"
    " WHEN m.heard_how != 'nearest' THEN m.heard_node_id END"
)
_HOW = (
    "CASE WHEN l.node_id IS NOT NULL THEN 'survey_link'"
    " WHEN m.heard_how = 'nearest' THEN 'unknown' ELSE m.heard_how END"
)
_GUESSED_SQL = f"({_HOW}) IN ({', '.join(repr(how) for how in GUESSED)})"
# The most certain way a node was identified in a cell comes first.
_HOW_ORDER: tuple[HeardHow, ...] = (
    "direct",
    "survey_link",
    "gateway_link",
    "reported",
    "sender_link",
    "only_candidate",
    "unknown",
)

# Whether a measurement is still true as far as SigSleuth can tell
# (docs/coverage.md). With its heard node in node_current, it counts from the
# node's last move on, as links.trusted_sql (though a doubtful
# position does not make a reception wrong). Otherwise it counts while
# recent (:alive_since, when node_current forgets a node), or while a survey
# has heard a packet the node sent since then: a survey reception is a
# sighting too, and absence from node_current only means MQTT cannot vouch
# for a node. Its own packets, not those it was the heard node of: the sender
# index answers in one seek, where the heard index would read every relayed
# measurement it was named for, for each measurement checked.
_ALIVE_SQL = f""" AND coalesce(
    (SELECT m.heard_at >= coalesce(n.moved_at, 0)
     FROM node_current AS n WHERE n.node_id = {_HEARD}),
    m.heard_at >= :alive_since OR EXISTS (
        SELECT 1 FROM coverage_measurement AS d
        WHERE d.sender_node_id = {_HEARD} AND d.heard_at >= :alive_since))"""

# A measurement has no channel of its own, so the map's channel filter drops
# those whose survey ran on another spreading factor than the channel's, or
# whose sender is known to be on another radio channel. A sender with no known
# channel stays when its survey fits: a survey where SigSleuth captures no
# nodes would otherwise show nothing. The + keeps SQLite on the cell index
# rather than probing every sender or session.
_CHANNEL_SQL = """ AND +m.sender_node_id NOT IN (
    SELECT node_id FROM node_current WHERE channel IS NOT NULL AND channel != :channel)
  AND +m.session_row_id NOT IN (
    SELECT id FROM coverage_session WHERE spreading_factor != :channel_sf)"""
# A node's measurements, by either index, for the node filter.
_NODE_SQL = f""" AND (m.heard_node_id = :node
    OR m.id IN (SELECT measurement_id FROM coverage_relay WHERE node_id = :node))
  AND {_HEARD} = :node"""


# A node that turned OK to MQTT off (opt_out.py) is never named, past
# measurements included: they colour cells and read as any unknown relay's,
# by relay byte (_SHOWN, _SHOWN_HOW), and selecting the node shows none.
_HEARD_OPTED_OUT = opted_out_sql(_HEARD)
_SHOWN = f"CASE WHEN {_HEARD_OPTED_OUT} THEN NULL ELSE {_HEARD} END"
_SHOWN_HOW = f"CASE WHEN {_HEARD_OPTED_OUT} THEN 'unknown' ELSE {_HOW} END"
_SHOWN_GUESSED = f"({_SHOWN_HOW}) IN ({', '.join(repr(how) for how in GUESSED)})"


def _point_filters(node: int | None, channel: str | None) -> str:
    """SQL conditions on _MEASUREMENTS for the map's filters and _ALIVE_SQL,
    shared by the cell list, one cell and a node's extent so they agree."""
    filters = _ALIVE_SQL
    if node is not None:
        filters += f"{_NODE_SQL} AND NOT {_HEARD_OPTED_OUT}"
    if channel is not None:
        filters += _CHANNEL_SQL
    return filters


def _filter_values(node: int | None, channel: str | None, alive_days: int) -> dict[str, Any]:
    """_point_filters' query values. alive_days is how long node_current
    remembers an unseen node (retention_days, 0 for ever); channel_sf is None
    for a channel whose settings are unknown, which drops no session."""
    preset = None if channel is None else preset_name(channel)
    return {
        "node": node,
        "channel": channel,
        "channel_sf": None if preset is None else PRESET_SPREADING_FACTORS[preset],
        "alive_since": now_ms() - alive_days * DAY_MS if alive_days else 0,
    }


def cell_level(zoom: float) -> int:
    """The cell level the map shows at a zoom."""
    return min(max(math.floor(zoom) + CELL_ZOOM_OFFSET, MIN_CELL_LEVEL), FINE_LEVEL)


@dataclass(frozen=True)
class CoverageCell:
    """One coverage cell at the list's level: the Web Mercator tile (x, y).
    average_margin and best_margin are the SNR margins above the survey
    node's floor, in dB; None when no measurement had one. spreading_factor
    is the one all its measurements' surveys ran on, None when they mix or
    one is unknown. nodes counts the distinct heard nodes; guessed counts
    measurements whose heard node is a guess (GUESSED)."""

    x: int
    y: int
    measurements: int
    average_margin: float | None
    best_margin: float | None
    average_snr: float | None
    spreading_factor: int | None
    nodes: int
    guessed: int


@dataclass(frozen=True)
class CoverageCellList:
    """The cells at `level` with measurements in the area, busiest first;
    truncated is true when more matched than MAX_CELLS."""

    level: int
    cells: tuple[CoverageCell, ...]
    truncated: bool


def coverage_cells(
    connection: sqlite3.Connection,
    *,
    west: float,
    south: float,
    east: float,
    north: float,
    zoom: float,
    since: datetime | None,
    node: int | None,
    channel: str | None = None,
    alive_days: int,
) -> CoverageCellList:
    """Coverage cells in an area for the map, from the measurements still
    true (_ALIVE_SQL; alive_days as in _filter_values). With node, only
    measurements where that node was the heard node count (its radio
    footprint, which includes its own packets heard directly). With channel,
    only measurements that can be on that channel count (_CHANNEL_SQL)."""
    level = cell_level(zoom)
    shift = FINE_LEVEL - level
    # Widen the area to whole cells at the level, so a cell at the edge of
    # the view counts all its measurements, as its detail does.
    x0, y0 = ((value >> shift) << shift for value in fine_cell(north, west))
    x1, y1 = ((((value >> shift) + 1) << shift) - 1 for value in fine_cell(south, east))
    filters = _point_filters(node, channel)
    # Consider aggregated cells if coverage_measurement grows past about 500k
    # rows: 200k synthetic points over a whole region took 270 ms, 420 ms
    # with _ALIVE_SQL and survey links (2026-10-16).
    rows = connection.execute(
        f"""
        SELECT m.cell_x >> :shift AS x, m.cell_y >> :shift AS y, count(*) AS measurements,
               avg(m.margin_db) AS average_margin, max(m.margin_db) AS best_margin,
               avg(m.rx_snr) AS average_snr,
               CASE WHEN count(s.spreading_factor) = count(*)
                    AND min(s.spreading_factor) = max(s.spreading_factor)
                    THEN min(s.spreading_factor) END AS spreading_factor,
               count(DISTINCT {_SHOWN}) AS nodes, sum({_SHOWN_GUESSED}) AS guessed
        FROM {_MEASUREMENTS}
        JOIN coverage_session AS s ON s.id = m.session_row_id
        WHERE m.cell_x BETWEEN :x0 AND :x1 AND m.cell_y BETWEEN :y0 AND :y1
          AND m.heard_at >= :since{filters}
        GROUP BY 1, 2
        ORDER BY measurements DESC, x, y
        LIMIT :limit
        """,
        {
            "shift": shift,
            "x0": x0,
            "x1": x1,
            "y0": y0,
            "y1": y1,
            "since": 0 if since is None else to_epoch_ms(since),
            "limit": MAX_CELLS + 1,
            **_filter_values(node, channel, alive_days),
        },
    ).fetchall()
    cells = tuple(
        CoverageCell(
            x=int(row["x"]),
            y=int(row["y"]),
            measurements=int(row["measurements"]),
            average_margin=_round(row["average_margin"]),
            best_margin=_round(row["best_margin"]),
            average_snr=_round(row["average_snr"]),
            spreading_factor=row["spreading_factor"],
            nodes=int(row["nodes"]),
            guessed=int(row["guessed"]),
        )
        for row in rows[:MAX_CELLS]
    )
    return CoverageCellList(level=level, cells=cells, truncated=len(rows) > MAX_CELLS)


@dataclass(frozen=True)
class CoverageExtent:
    """The area holding every fine cell where a node was heard, for the maps
    to fit its coverage in view."""

    west: float
    south: float
    east: float
    north: float


def coverage_extent(
    connection: sqlite3.Connection,
    node: int,
    *,
    channel: str | None = None,
    alive_days: int,
) -> CoverageExtent | None:
    """Where survey nodes heard a node, as in coverage_cells with node and
    channel; None when none did."""
    row = connection.execute(
        f"""
        SELECT min(m.cell_x) AS x0, max(m.cell_x) AS x1, min(m.cell_y) AS y0, max(m.cell_y) AS y1
        FROM {_MEASUREMENTS} WHERE 1{_point_filters(node, channel)}
        """,
        _filter_values(node, channel, alive_days),
    ).fetchone()
    if row["x0"] is None:
        return None
    north, west = tile_point(FINE_LEVEL, row["x0"], row["y0"])
    south, east = tile_point(FINE_LEVEL, row["x1"] + 1, row["y1"] + 1)
    return CoverageExtent(west=west, south=south, east=east, north=north)


MAX_VIEW_NODES = 200


@dataclass(frozen=True)
class CoverageViewNode:
    """A node heard in an area: cells counts the fine cells it was heard in,
    average_margin is as cells average it."""

    node_id: int
    long_name: str | None
    short_name: str | None
    cells: int
    measurements: int
    average_margin: float | None


@dataclass(frozen=True)
class CoverageNodeList:
    """The nodes heard in an area, in most cells first, at most
    MAX_VIEW_NODES (truncated when more were); unknown counts the
    measurements with no heard node shown."""

    nodes: tuple[CoverageViewNode, ...]
    unknown: int
    truncated: bool


def coverage_nodes(
    connection: sqlite3.Connection,
    *,
    west: float,
    south: float,
    east: float,
    north: float,
    channel: str | None = None,
    alive_days: int,
) -> CoverageNodeList:
    """The nodes heard in the fine cells overlapping an area, from the
    measurements coverage_cells counts with channel and no node."""
    x0, y0 = fine_cell(north, west)
    x1, y1 = fine_cell(south, east)
    # The unknown group sorts first so the limit never cuts it.
    rows = connection.execute(
        f"""
        SELECT {_SHOWN} AS shown, count(DISTINCT (m.cell_x << {FINE_LEVEL}) | m.cell_y) AS cells,
               count(*) AS measurements, avg(m.margin_db) AS average_margin
        FROM {_MEASUREMENTS}
        WHERE m.cell_x BETWEEN :x0 AND :x1 AND m.cell_y BETWEEN :y0 AND :y1
          {_point_filters(None, channel)}
        GROUP BY 1
        ORDER BY shown IS NOT NULL, cells DESC, measurements DESC, shown
        LIMIT :limit
        """,
        {
            "x0": x0,
            "x1": x1,
            "y0": y0,
            "y1": y1,
            "limit": MAX_VIEW_NODES + 2,
            **_filter_values(None, channel, alive_days),
        },
    ).fetchall()
    unknown = int(rows[0]["measurements"]) if rows and rows[0]["shown"] is None else 0
    heard = [row for row in rows if row["shown"] is not None]
    names = nodes_by_id(connection, {int(row["shown"]) for row in heard[:MAX_VIEW_NODES]})
    nodes = []
    for row in heard[:MAX_VIEW_NODES]:
        summary = names.get(int(row["shown"]))
        nodes.append(
            CoverageViewNode(
                node_id=int(row["shown"]),
                long_name=None if summary is None else summary.long_name,
                short_name=None if summary is None else summary.short_name,
                cells=int(row["cells"]),
                measurements=int(row["measurements"]),
                average_margin=_round(row["average_margin"]),
            )
        )
    return CoverageNodeList(
        nodes=tuple(nodes), unknown=unknown, truncated=len(heard) > MAX_VIEW_NODES
    )


@dataclass(frozen=True)
class CoverageHeardNode:
    """A node heard in a cell. how is the most certain way it was identified
    there, candidates the most candidates the rule-outs left for one of its
    measurements (heard_node), and guessed counts its measurements identified
    by a guess. The map draws lines to it from its own nodes' positions,
    which include estimates. average_snr is the
    survey nodes' SNR as radios show it; a node transmits on one network, so
    it mixes no radio settings. average_margin is the SNR above the survey
    node's floor, as cells average it. channel is the node's latest channel,
    None when unknown."""

    node_id: int
    long_name: str | None
    short_name: str | None
    how: HeardHow
    candidates: int
    measurements: int
    guessed: int
    average_snr: float | None
    average_margin: float | None
    channel: str | None
    last_heard_at: datetime


@dataclass(frozen=True)
class CoverageUnknownRelay:
    """Measurements in a cell whose heard node is unknown, by relay byte
    (None when the packet carried none), with the nodes that may have relayed
    them (relay_candidates; for the CELL_CANDIDATE_RELAYS commonest)."""

    relay_byte: int | None
    measurements: int
    candidates: tuple[CoverageCandidate, ...]


@dataclass(frozen=True)
class CoverageMeasurementRow:
    """One measurement in a cell, named by the node heard, not the sender.
    packet_row_id is the packet MQTT saved with the same sender and packet
    ID, when there is one that pages show (opt_out.SHOWN_PACKET). margin is
    rx_snr above the survey node's floor, None when either is unknown."""

    heard_at: datetime
    heard_node_id: int | None
    heard_name: str | None
    heard_how: HeardHow
    relay_byte: int | None
    rx_snr: float | None
    rx_rssi: int | None
    margin: float | None
    packet_row_id: int | None


@dataclass(frozen=True)
class CoverageCellNetwork:
    """A cell's measurements from survey sessions with one set of radio
    settings (None where a session did not say). SNR is only comparable
    within the same spreading factor, so a cell's SNR is totalled per
    network; the map's colours use margins, which compare across them.
    average_margin is the network's average SNR above the survey node's
    floor."""

    spreading_factor: int | None
    bandwidth_khz: float | None
    frequency_mhz: float | None
    measurements: int
    average_snr: float | None
    average_margin: float | None


@dataclass(frozen=True)
class CoverageCellDetail:
    """One cell: its centre and approximate width (size_m), its totals, the
    nodes heard there (at most CELL_MAX_NODES, most measurements first), the
    unknown relays and the newest CELL_NEWEST measurements. measurements is 0
    when the cell has none in the window. networks totals the SNR per radio
    settings, most measurements first."""

    level: int
    x: int
    y: int
    latitude: float
    longitude: float
    size_m: float
    measurements: int
    networks: tuple[CoverageCellNetwork, ...]
    first_heard_at: datetime | None
    last_heard_at: datetime | None
    heard_nodes: tuple[CoverageHeardNode, ...]
    unknown_relays: tuple[CoverageUnknownRelay, ...]
    newest: tuple[CoverageMeasurementRow, ...]


def coverage_cell(
    connection: sqlite3.Connection,
    level: int,
    x: int,
    y: int,
    *,
    since: datetime | None,
    node: int | None = None,
    channel: str | None = None,
    alive_days: int,
) -> CoverageCellDetail:
    """One cell's totals, heard nodes, unknown relays and newest measurements,
    from the measurements still true and filtered by node and channel as in
    coverage_cells."""
    shift = FINE_LEVEL - level
    where = """
        m.cell_x BETWEEN :x0 AND :x1 AND m.cell_y BETWEEN :y0 AND :y1 AND m.heard_at >= :since
    """ + _point_filters(node, channel)
    values = {
        "x0": x << shift,
        "x1": ((x + 1) << shift) - 1,
        "y0": y << shift,
        "y1": ((y + 1) << shift) - 1,
        "since": 0 if since is None else to_epoch_ms(since),
        **_filter_values(node, channel, alive_days),
    }
    totals = connection.execute(
        f"""
        SELECT count(*) AS measurements, min(m.heard_at) AS first_heard_at,
               max(m.heard_at) AS last_heard_at
        FROM {_MEASUREMENTS} WHERE {where}
        """,
        values,
    ).fetchone()
    networks = connection.execute(
        f"""
        SELECT s.spreading_factor, s.bandwidth_khz, s.frequency_mhz, count(*) AS measurements,
               avg(m.rx_snr) AS average_snr, avg(m.margin_db) AS average_margin
        FROM {_MEASUREMENTS}
        JOIN coverage_session AS s ON s.id = m.session_row_id
        WHERE {where}
        GROUP BY 1, 2, 3 ORDER BY measurements DESC
        """,
        values,
    ).fetchall()
    how_rank = " ".join(f"WHEN '{how}' THEN {rank}" for rank, how in enumerate(_HOW_ORDER))
    heard_rows = connection.execute(
        f"""
        SELECT {_HEARD} AS heard_node_id, count(*) AS measurements,
               sum({_GUESSED_SQL}) AS guessed,
               avg(m.rx_snr) AS average_snr, avg(m.margin_db) AS average_margin,
               max(m.heard_at) AS last_heard_at,
               min(CASE {_HOW} {how_rank} END) AS how_rank,
               max(m.candidates) AS candidates,
               (SELECT channel FROM node_current WHERE node_id = {_HEARD}) AS channel
        FROM {_MEASUREMENTS}
        WHERE {where} AND {_HEARD} IS NOT NULL AND NOT {_HEARD_OPTED_OUT}
        GROUP BY 1
        ORDER BY measurements DESC, 1
        LIMIT {CELL_MAX_NODES}
        """,
        values,
    ).fetchall()
    unknown = connection.execute(
        f"""
        SELECT m.relay_byte, count(*) AS measurements
        FROM {_MEASUREMENTS}
        WHERE {where} AND {_SHOWN} IS NULL
        GROUP BY m.relay_byte ORDER BY measurements DESC, m.relay_byte
        """,
        values,
    ).fetchall()
    newest = connection.execute(
        f"""
        SELECT m.heard_at, m.sender_node_id, {_SHOWN} AS heard_node_id,
               {_SHOWN_HOW} AS heard_how, m.packet_id, m.relay_byte, m.rx_snr, m.rx_rssi,
               m.margin_db
        FROM {_MEASUREMENTS}
        WHERE {where}
        ORDER BY m.heard_at DESC LIMIT {CELL_NEWEST}
        """,
        values,
    ).fetchall()
    heard_ids = {int(row["heard_node_id"]) for row in heard_rows}
    names = nodes_by_id(
        connection,
        heard_ids
        | {int(row["heard_node_id"]) for row in newest if row["heard_node_id"] is not None},
    )
    latitude, longitude = tile_centre(level, x, y)
    relay_bytes = [
        int(row["relay_byte"]) for row in unknown[:CELL_CANDIDATE_RELAYS] if row["relay_byte"]
    ]
    spreading_factors = {
        int(row["spreading_factor"]) for row in networks if row["spreading_factor"]
    }
    candidates = dict(
        zip(
            relay_bytes,
            relay_candidates(
                connection,
                [(byte, latitude, longitude, spreading_factors, set()) for byte in relay_bytes],
            ),
            strict=True,
        )
    )
    return CoverageCellDetail(
        level=level,
        x=x,
        y=y,
        latitude=round(latitude, 6),
        longitude=round(longitude, 6),
        size_m=round(EARTH_CIRCUMFERENCE_M * math.cos(math.radians(latitude)) / (1 << level)),
        measurements=int(totals["measurements"]),
        networks=tuple(
            CoverageCellNetwork(
                spreading_factor=row["spreading_factor"],
                bandwidth_khz=row["bandwidth_khz"],
                frequency_mhz=row["frequency_mhz"],
                measurements=int(row["measurements"]),
                average_snr=_round(row["average_snr"]),
                average_margin=_round(row["average_margin"]),
            )
            for row in networks
        ),
        first_heard_at=optional_from_epoch_ms(totals["first_heard_at"]),
        last_heard_at=optional_from_epoch_ms(totals["last_heard_at"]),
        heard_nodes=tuple(
            _heard_node(row, names.get(int(row["heard_node_id"]))) for row in heard_rows
        ),
        unknown_relays=tuple(
            CoverageUnknownRelay(
                row["relay_byte"],
                int(row["measurements"]),
                candidates.get(row["relay_byte"], ()),
            )
            for row in unknown
        ),
        newest=tuple(_measurement_row(connection, row, names) for row in newest),
    )


def relay_candidates(
    connection: sqlite3.Connection,
    places: Sequence[tuple[int, float, float, set[int], set[int]]],
) -> list[tuple[CoverageCandidate, ...]]:
    """Up to CELL_CANDIDATES possible relays for each (byte, latitude,
    longitude, spreading factors, excluded nodes), nearest first. Hints only;
    nodes that turned OK to MQTT off are never suggested."""
    if not places:
        return []
    relay_bytes = {byte for byte, _, _, _, _ in places}
    by_byte = nodes_by_byte(connection, relay_bytes)
    nodes = {node for found in by_byte.values() for node in found}
    hidden = opted_out(connection)
    by_byte = {byte: [n for n in found if n not in hidden] for byte, found in by_byte.items()}
    nodes -= hidden
    placed, radios = nodes_by_id(connection, nodes), node_radios(connection, nodes)
    reach = own_reach(connection, nodes, from_km=RULE_OUT_KM)
    found = []
    for relay_byte, latitude, longitude, spreading_factors, excluded in places:
        centre = (latitude, longitude)
        near = []
        for node in by_byte.get(relay_byte, []):
            if node in excluded:
                continue
            place = placed.get(node)
            km = node_km(place, centre)
            if place is None or km is None:
                continue
            reach_km = max(reach.get(node, 0), RULE_OUT_KM)
            if not cannot_relay_at(place, radios.get(node), reach_km, centre, spreading_factors):
                near.append(
                    CoverageCandidate(node, place.long_name, place.short_name, round(km, 1))
                )
        near.sort(key=lambda candidate: (candidate.km, candidate.node_id))
        found.append(tuple(near[:CELL_CANDIDATES]))
    return found


@dataclass(frozen=True)
class CoverageRelayNode:
    """A node that could relay a survey's packets, where the map draws it,
    and its reach (positions.own_reach) when longer than RULE_OUT_KM, within
    which any node could relay it."""

    node_id: int
    long_name: str | None
    short_name: str | None
    latitude: float
    longitude: float
    estimated: bool
    reach_km: float | None


@dataclass(frozen=True)
class CoverageRelayNodeList:
    nodes: list[CoverageRelayNode]
    truncated: bool


def relay_nodes(
    connection: sqlite3.Connection,
    *,
    spreading_factor: int | None,
    limit: int = MAX_RELAY_NODES,
) -> CoverageRelayNodeList:
    """The placed nodes that could relay a packet on spreading_factor
    (relays.cannot_relay_at, at any distance), most recently seen first, for
    the survey app's relay guesses while offline. Nodes that turned
    OK to MQTT off are left out, as they are never named."""
    hidden = opted_out(connection)
    seen = [
        int(row[0])
        for row in connection.execute(
            "SELECT node_id FROM node_current ORDER BY last_seen_at DESC, node_id"
        )
        if int(row[0]) not in hidden
    ]
    placed, radios = nodes_by_id(connection, set(seen)), node_radios(connection, set(seen))
    factors = set() if spreading_factor is None else {spreading_factor}
    able = [
        node
        for node in seen
        if not cannot_relay_at(placed.get(node), radios.get(node), math.inf, None, factors)
    ]
    shown = able[:limit]
    reach = own_reach(connection, shown, from_km=RULE_OUT_KM)
    nodes = []
    for node in shown:
        place = placed[node]
        assert place.latitude is not None and place.longitude is not None
        nodes.append(
            CoverageRelayNode(
                node_id=node,
                long_name=place.long_name,
                short_name=place.short_name,
                latitude=place.latitude,
                longitude=place.longitude,
                estimated=place.estimated,
                reach_km=round(reach[node], 1) if node in reach else None,
            )
        )
    return CoverageRelayNodeList(nodes=nodes, truncated=len(able) > limit)


def tile_centre(level: int, x: int, y: int) -> tuple[float, float]:
    """The latitude and longitude of a Web Mercator tile's centre."""
    return tile_point(level, x + 0.5, y + 0.5)


def tile_point(level: int, x: float, y: float) -> tuple[float, float]:
    """The latitude and longitude of a point in Web Mercator tile units:
    (x, y) is a tile's north-west corner."""
    scale = 1 << level
    longitude = x / scale * 360 - 180
    latitude = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / scale))))
    return latitude, longitude


def _heard_node(row: sqlite3.Row, summary: NodeSummary | None) -> CoverageHeardNode:
    node_id = int(row["heard_node_id"])
    return CoverageHeardNode(
        node_id=node_id,
        long_name=None if summary is None else summary.long_name,
        short_name=None if summary is None else summary.short_name,
        how=_HOW_ORDER[int(row["how_rank"])],
        candidates=int(row["candidates"]),
        measurements=int(row["measurements"]),
        guessed=int(row["guessed"]),
        average_snr=_round(row["average_snr"]),
        average_margin=_round(row["average_margin"]),
        channel=row["channel"],
        last_heard_at=from_epoch_ms(row["last_heard_at"]),
    )


def _measurement_row(
    connection: sqlite3.Connection, row: sqlite3.Row, names: Mapping[int, NodeSummary]
) -> CoverageMeasurementRow:
    packet = None
    if row["packet_id"] is not None:
        packet = connection.execute(
            f"""
            SELECT id FROM packet INDEXED BY packet_identity_time
            WHERE sender_node_id = :sender AND packet_id = :packet
              AND last_received_at >= :heard_at - :window
              AND first_received_at <= :heard_at + :window AND {SHOWN_PACKET}
            ORDER BY id LIMIT 1
            """,
            {
                "sender": row["sender_node_id"],
                "packet": row["packet_id"],
                "heard_at": row["heard_at"],
                "window": MQTT_MATCH_MS,
            },
        ).fetchone()
    heard = row["heard_node_id"]
    return CoverageMeasurementRow(
        heard_at=from_epoch_ms(row["heard_at"]),
        heard_node_id=heard,
        heard_name=None if heard is None else _name(names.get(int(heard))),
        heard_how=row["heard_how"],
        relay_byte=row["relay_byte"],
        rx_snr=row["rx_snr"],
        rx_rssi=row["rx_rssi"],
        margin=_round(row["margin_db"]),
        packet_row_id=None if packet is None else int(packet[0]),
    )


def _name(summary: NodeSummary | None) -> str | None:
    return None if summary is None else summary.long_name or summary.short_name


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
