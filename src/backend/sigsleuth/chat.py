"""The Chat page: readable text messages as a conversation, and their channels."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from sigsleuth.db import fold, from_epoch_ms, optional_text
from sigsleuth.links import link_trust
from sigsleuth.radio import great_circle_km

# A quoted parent's text is cut to about this many characters.
CHAT_SNIPPET_LENGTH = 80

# A reply whose parent is not saved as readable text looks for any saved packet
# with that packet ID among this many packet rows before it: over 99% of live
# replies come this close to their parent.
PARENT_SEARCH_ROWS = 20_000


@dataclass(frozen=True)
class ChatParent:
    """The saved message a reply or reaction answers; text is a snippet, None
    when the packet was saved but could not be read (an unknown channel key)."""

    packet_row_id: int
    sender_node_id: int
    sender_short_name: str | None
    sender_long_name: str | None
    text: str | None


@dataclass(frozen=True)
class ChatMessage:
    """One readable text message.

    recipient_node_id is None for a broadcast. reply_id is the packet ID it
    answers, and reply_to the saved message with that packet ID, None when none
    is saved. farthest_km is how far from the sender the farthest reporting
    gateway is, between reported positions as they are now (estimates would
    measure their own guess). Like the map, it leaves out the
    nodes whose position no longer matches the message (links.link_trust): a
    gateway that moved since, or a doubtful one. None when the sender is left
    out or has no position, or every other gateway is or has none.
    """

    packet_row_id: int
    packet_id: int
    received_at: datetime
    sender_node_id: int
    sender_short_name: str | None
    sender_long_name: str | None
    recipient_node_id: int | None
    recipient_short_name: str | None
    recipient_long_name: str | None
    channel: str
    text: str
    is_reaction: bool
    reply_id: int | None
    reply_to: ChatParent | None
    reception_count: int
    farthest_km: float | None


@dataclass(frozen=True)
class ChatPage:
    messages: tuple[ChatMessage, ...]
    next_before_id: int | None
    has_newer: bool


def chat_messages(
    connection: sqlite3.Connection,
    *,
    limit: int,
    before_id: int | None = None,
    after_id: int | None = None,
    channel: str | None = None,
    search: str | None = None,
    hidden_senders: tuple[int, ...] = (),
) -> ChatPage:
    """A page of readable text messages, oldest first, ordered by packet row ID
    (about the first receipt time). The partial text_message indexes keep this
    fast however many other packets there are.

    search matches part of the text or of the sender's name, ignoring case and
    accents. It reads every text message when few match, most of the time
    folding the texts; a plain LIKE is faster but misses accents. Hidden
    senders are left out."""
    # portnum = 1 (text messages) is written out in these queries, as in the
    # partial indexes, so SQLite can use them.
    filters = ["t.portnum = 1", "t.application_payload IS NOT NULL"]
    if channel is not None:
        filters.append("t.channel = :channel")
    if search is not None and search.strip():
        filters.append(
            """(instr(fold(t.application_payload), :search) > 0 OR t.sender_node_id IN (
                SELECT node_id FROM node_current
                WHERE instr(fold(short_name), :search) > 0 OR instr(fold(long_name), :search) > 0
            ))"""
        )
    if hidden_senders:
        filters.append("t.sender_node_id NOT IN (SELECT value FROM json_each(:hidden))")
    if after_id is not None:
        filters.append("t.id > :after")
        order = "ASC"
    else:
        filters.append("t.id < :before")
        order = "DESC"
    rows = connection.execute(
        f"""
        SELECT t.id, t.packet_id, t.first_received_at, t.sender_node_id, t.recipient_node_id,
               t.channel, t.application_payload, t.is_reaction, t.reply_id,
               t.reception_count,
               sender.short_name AS sender_short_name, sender.long_name AS sender_long_name,
               sender.latitude AS sender_latitude, sender.longitude AS sender_longitude,
               recipient.short_name AS recipient_short_name,
               recipient.long_name AS recipient_long_name
        FROM packet AS t
        LEFT JOIN node_current AS sender ON sender.node_id = t.sender_node_id
        LEFT JOIN node_current AS recipient ON recipient.node_id = t.recipient_node_id
        WHERE {" AND ".join(filters)}
        ORDER BY t.id {order}
        LIMIT :limit
        """,
        {
            "channel": channel,
            "search": fold(search.strip()) if search is not None else None,
            "hidden": json.dumps(hidden_senders),
            "after": after_id,
            "before": before_id if before_id is not None else 2**63 - 1,
            "limit": limit + 1,
        },
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    if after_id is None:
        rows.reverse()
    farthest = _farthest_gateways(connection, rows)
    parents = _chat_parents(connection, {int(row["reply_id"]) for row in rows if row["reply_id"]})
    for row in rows:
        reply_id = row["reply_id"]
        if reply_id and reply_id not in parents:
            parent = _unreadable_parent(connection, int(row["id"]), int(reply_id))
            if parent is not None:
                parents[int(reply_id)] = parent
    messages = tuple(
        ChatMessage(
            packet_row_id=int(row["id"]),
            packet_id=int(row["packet_id"]),
            received_at=from_epoch_ms(row["first_received_at"]),
            sender_node_id=int(row["sender_node_id"]),
            sender_short_name=optional_text(row["sender_short_name"]),
            sender_long_name=optional_text(row["sender_long_name"]),
            recipient_node_id=(
                None if row["recipient_node_id"] == 0xFFFFFFFF else int(row["recipient_node_id"])
            ),
            recipient_short_name=optional_text(row["recipient_short_name"]),
            recipient_long_name=optional_text(row["recipient_long_name"]),
            channel=str(row["channel"]),
            text=_text(row["application_payload"]),
            is_reaction=bool(row["is_reaction"]),
            reply_id=row["reply_id"],
            reply_to=None if row["reply_id"] is None else parents.get(int(row["reply_id"])),
            reception_count=int(row["reception_count"]),
            farthest_km=farthest.get(int(row["id"])),
        )
        for row in rows
    )
    return ChatPage(
        messages=messages,
        next_before_id=messages[0].packet_row_id if has_more and after_id is None else None,
        has_newer=has_more and after_id is not None,
    )


def _farthest_gateways(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[int, float]:
    """Per message, the km from its placed sender to its farthest placed
    reporting gateway other than the sender itself, both trusted at the
    message's time. Looking up where every gateway of 100 messages was when
    heard would cost about a second, so this uses where they are now."""
    if not rows:
        return {}
    trust = link_trust(connection, min(int(row["first_received_at"]) for row in rows))
    sent_at = {int(row["id"]): int(row["first_received_at"]) for row in rows}
    senders = {
        int(row["id"]): (
            int(row["sender_node_id"]),
            row["sender_latitude"],
            row["sender_longitude"],
        )
        for row in rows
        if row["sender_latitude"] is not None
        and trust.get(int(row["sender_node_id"]), 0) <= sent_at[int(row["id"])]
    }
    if not senders:
        return {}
    gateways = connection.execute(
        """
        SELECT r.packet_row_id, r.gateway_node_id, g.latitude, g.longitude
        FROM reception AS r
        JOIN node_current AS g ON g.node_id = r.gateway_node_id
        WHERE r.packet_row_id IN (SELECT value FROM json_each(?)) AND g.latitude IS NOT NULL
        """,
        (json.dumps(sorted(senders)),),
    ).fetchall()
    farthest: dict[int, float] = {}
    for packet_row_id, gateway, latitude, longitude in gateways:
        sender, sender_latitude, sender_longitude = senders[packet_row_id]
        if gateway != sender and trust.get(gateway, 0) <= sent_at[packet_row_id]:
            km = great_circle_km(sender_latitude, sender_longitude, latitude, longitude)
            farthest[packet_row_id] = max(km, farthest.get(packet_row_id, 0.0))
    return farthest


def _chat_parents(connection: sqlite3.Connection, packet_ids: set[int]) -> dict[int, ChatParent]:
    """Saved text messages by packet ID; the newest wins. Packet IDs are 32-bit and
    random, so a collision among text messages is rare enough to ignore."""
    if not packet_ids:
        return {}
    rows = connection.execute(
        f"""
        SELECT t.id, t.packet_id, t.sender_node_id, t.application_payload, node.short_name,
               node.long_name
        FROM packet AS t
        LEFT JOIN node_current AS node ON node.node_id = t.sender_node_id
        WHERE t.portnum = 1 AND t.packet_id IN ({",".join("?" * len(packet_ids))})
          AND t.application_payload IS NOT NULL
        ORDER BY t.id
        """,
        tuple(packet_ids),
    ).fetchall()
    parents = {}
    for row in rows:
        text = _text(row["application_payload"])
        if len(text) > CHAT_SNIPPET_LENGTH:
            text = text[: CHAT_SNIPPET_LENGTH - 1] + "…"
        parents[int(row["packet_id"])] = ChatParent(
            packet_row_id=int(row["id"]),
            sender_node_id=int(row["sender_node_id"]),
            sender_short_name=optional_text(row["short_name"]),
            sender_long_name=optional_text(row["long_name"]),
            text=text,
        )
    return parents


def _unreadable_parent(
    connection: sqlite3.Connection, packet_row_id: int, packet_id: int
) -> ChatParent | None:
    """The nearest saved packet with packet_id shortly before a reply, readable or
    not. Without a packet_id index over all ports this reads a bounded window."""
    row = connection.execute(
        """
        SELECT t.id, t.sender_node_id, node.short_name, node.long_name
        FROM packet AS t
        LEFT JOIN node_current AS node ON node.node_id = t.sender_node_id
        WHERE t.id BETWEEN :first AND :last AND t.packet_id = :packet_id
        ORDER BY t.id DESC
        LIMIT 1
        """,
        {
            "first": packet_row_id - PARENT_SEARCH_ROWS,
            "last": packet_row_id,
            "packet_id": packet_id,
        },
    ).fetchone()
    if row is None:
        return None
    return ChatParent(
        packet_row_id=int(row["id"]),
        sender_node_id=int(row["sender_node_id"]),
        sender_short_name=optional_text(row["short_name"]),
        sender_long_name=optional_text(row["long_name"]),
        text=None,
    )


def _text(payload: bytes) -> str:
    return bytes(payload).decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ChatChannel:
    channel: str
    messages: int


@dataclass(frozen=True)
class ChatChannelList:
    channels: tuple[ChatChannel, ...]


def chat_channels(connection: sqlite3.Connection) -> ChatChannelList:
    """Text message counts per channel, from the (channel, id) text index alone."""
    rows = connection.execute(
        """
        SELECT channel, count(*) AS messages
        FROM packet
        WHERE portnum = 1
        GROUP BY channel
        ORDER BY messages DESC, channel
        """
    ).fetchall()
    return ChatChannelList(
        tuple(ChatChannel(str(row["channel"]), int(row["messages"])) for row in rows)
    )
