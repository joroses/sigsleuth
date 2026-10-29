"""The requests the pages make, shared by the query plan and API snapshot tests
and scripts/perf.py.

When you add a page or a request, add its URL here so all of them cover it.

scripts/perf.py fails when a request's median on data/perf.db takes longer than
its budget: BUDGET_MS, or the larger one yielded beside a request that is heavy
by design. Give a request more time only with a reason beside it.
"""

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from meshtastic.protobuf import portnums_pb2  # type: ignore[import-untyped]

from sigsleuth.db import first_packet_row_id_since, now_ms, open_current_database

# Budgets are medians on a development machine on data/perf.db, a copy of a busy
# instance's database (docs/setup.md). A small server runs about 3.5
# times slower, so 150 ms here is about half a second there.
BUDGET_MS = 150
# Live 2026-10-12: traceroute 287 ms, direct 210 ms, neighbour 143 ms.
WEEK_LINKS_BUDGET_MS = {"traceroute": 400, "direct": 300, "neighbor": 200}
# About seven hours of live loranet.pl, most of the recorded sample.
FLOOD_PACKETS = 20_000


@dataclass(frozen=True)
class Subjects:
    """What the requests ask about: a node with a traceroute, a node next to it
    on a route, part of the first node's name, a channel, a busy text sender,
    a packet, one relayed the most, a traceroute request and a relayed
    reception of it."""

    node: int
    neighbour: int
    name: str
    channel: str
    text_sender: int
    packet: int = 1
    flood_packet: int = 1
    traceroute_packet: int = 1
    relayed_reception: int = 1


def pick_subjects(database_path: Path) -> Subjects:
    """The named node in the most traceroute hops among those that sent packets,
    its most frequent hop partner, its short name, the busiest channel,
    the busiest broadcast-text sender in the requests' 24-hour window, the
    oldest packet retention kept, of the newest FLOOD_PACKETS, the one
    other than a traceroute with the most relayed receptions, the
    traceroute request with the most hops and its first reception relayed
    by another node: chosen by rule, so a new sample needs no edits."""
    with closing(open_current_database(database_path)) as connection:
        node, short_name = connection.execute(
            """
            SELECT hop.node_id, node_current.short_name
            FROM (
                SELECT from_node_id AS node_id FROM traceroute_hop
                UNION ALL SELECT to_node_id FROM traceroute_hop
            ) AS hop
            JOIN node_current USING (node_id)
            WHERE node_current.short_name IS NOT NULL AND node_current.last_sent_at IS NOT NULL
            GROUP BY hop.node_id
            ORDER BY count(*) DESC, hop.node_id
            LIMIT 1
            """
        ).fetchone()
        (neighbour,) = connection.execute(
            """
            SELECT CASE WHEN from_node_id = :node THEN to_node_id ELSE from_node_id END AS other
            FROM traceroute_hop
            WHERE :node IN (from_node_id, to_node_id)
            GROUP BY other
            ORDER BY count(*) DESC, other
            LIMIT 1
            """,
            {"node": node},
        ).fetchone()
        (channel,) = connection.execute(
            "SELECT channel FROM packet GROUP BY channel ORDER BY count(*) DESC, channel LIMIT 1"
        ).fetchone()
        since = now_ms() - 86_400_000
        since_id = first_packet_row_id_since(connection, since - since % 3_600_000)
        (text_sender,) = connection.execute(
            """
            SELECT sender_node_id FROM packet
            WHERE portnum = 1 AND recipient_node_id = 4294967295 AND id >= ?
              AND EXISTS (
                  SELECT 1 FROM reception
                  WHERE packet_row_id = packet.id AND gateway_node_id != sender_node_id
              )
            GROUP BY sender_node_id
            ORDER BY count(*) DESC, sender_node_id
            LIMIT 1
            """,
            (since_id,),
        ).fetchone()
        (packet,) = connection.execute("SELECT min(id) FROM packet").fetchone()
        (flood_packet,) = connection.execute(
            """
            SELECT r.packet_row_id FROM packet AS t JOIN reception AS r ON r.packet_row_id = t.id
            WHERE t.id >= coalesce((
                SELECT id FROM packet ORDER BY id DESC LIMIT 1 OFFSET ?
            ), 0) AND t.portnum != ?
              AND r.hop_start > r.hop_limit
            GROUP BY r.packet_row_id
            ORDER BY count(*) DESC, r.packet_row_id
            LIMIT 1
            """,
            (FLOOD_PACKETS - 1, portnums_pb2.PortNum.TRACEROUTE_APP),
        ).fetchone() or (packet,)
        (traceroute_packet,) = connection.execute(
            """
            SELECT h.packet_row_id FROM traceroute_hop AS h
            JOIN traceroute_packet AS tr USING (packet_row_id)
            WHERE NOT tr.is_reply
            GROUP BY h.packet_row_id
            ORDER BY count(*) DESC, h.packet_row_id
            LIMIT 1
            """
        ).fetchone()
        (relayed_reception,) = connection.execute(
            """
            SELECT r.id FROM reception AS r JOIN packet AS t ON t.id = r.packet_row_id
            WHERE r.packet_row_id = ? AND r.relay_byte > 0 AND r.hop_start > r.hop_limit
              AND r.relay_byte NOT IN (r.gateway_node_id & 255, t.sender_node_id & 255)
            ORDER BY r.id
            LIMIT 1
            """,
            (traceroute_packet,),
        ).fetchone() or (1,)
    return Subjects(
        node,
        neighbour,
        short_name,
        channel,
        text_sender,
        packet,
        flood_packet,
        traceroute_packet,
        relayed_reception,
    )


def page_requests(now: datetime, subjects: Subjects) -> Iterator[str]:
    """The requests the pages make, with the parameters they send."""
    for url, _ in budgeted_page_requests(now, subjects):
        yield url


def budgeted_page_requests(now: datetime, subjects: Subjects) -> Iterator[tuple[str, int]]:
    """Each request with its time budget in milliseconds."""
    for request in requests(now, subjects):
        yield request if isinstance(request, tuple) else (request, BUDGET_MS)


def requests(now: datetime, subjects: Subjects) -> Iterator[str | tuple[str, int]]:
    """A URL, or (URL, budget) when the request may take longer than BUDGET_MS."""
    day = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    three_days = (now - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    week = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    fortnight = (now - timedelta(days=14)).isoformat().replace("+00:00", "Z")
    text = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    node, neighbour = subjects.node, subjects.neighbour
    name, channel = quote(subjects.name), quote(subjects.channel)
    yield "/api/v1/packets?limit=50"
    yield f"/api/v1/packets?limit=50&port={text}"
    yield "/api/v1/packets?limit=50&port=-1"
    yield f"/api/v1/packets?limit=50&sender={node}"
    # Diagnostics' packets of one direction of a long link.
    yield f"/api/v1/packets?limit=50&sender={node}&heard_directly_by={neighbour}"
    yield "/api/v1/packets/ports"
    yield f"/api/v1/packets/{subjects.packet}"
    yield f"/api/v1/packets/{subjects.traceroute_packet}"
    # Live copy 2026-10-23: 220-245 ms for broadcasts heard by 61-64
    # gateways. Asked for only when the relay map opens.
    yield f"/api/v1/packets/{subjects.flood_packet}/flood-routes", 400
    for sort in ("last_sent", "traceroute", "packets", "texts"):
        yield f"/api/v1/nodes?sort={sort}&limit=20&offset=0"
    for sort in (
        "channel_utilization",
        "air_util_tx",
        "firmware",
        "firmware_oldest",
        "altitude",
        "altitude_lowest",
    ):
        yield f"/api/v1/nodes?sort={sort}&limit=20&offset=0"
    yield f"/api/v1/nodes?sort=traceroute&limit=20&offset=0&q={name}&channel={channel}&gateway=true"
    yield "/api/v1/nodes/channels"
    yield f"/api/v1/nodes/{node}"
    yield f"/api/v1/nodes/{node}/packets?limit=50"
    yield f"/api/v1/nodes/{node}/packets?limit=50&port={text}"
    # A shared node number's radio filter (the sample has none:
    # tests/test_shared_numbers.py).
    yield f"/api/v1/nodes/{node}/packets?limit=50&radio=1"
    yield f"/api/v1/nodes/{node}/packets?limit=50&radio=unsorted"
    yield f"/api/v1/nodes/{node}/charts?since={week}"
    yield f"/api/v1/nodes/{node}/positions"
    yield f"/api/v1/nodes/{node}/contacts?since={week}"
    yield f"/api/v1/nodes/{node}/links?since={week}"
    yield "/api/v1/map/nodes"
    yield f"/api/v1/map/nodes?seen_since={day}&position_since={fortnight}"
    # What the map opens on: the last 3 days, traceroute links only. Its
    # links add up every hourly row of the 3 days (549 ms live 2026-10-12).
    yield f"/api/v1/map/nodes?seen_since={three_days}&position_since={fortnight}"
    yield f"/api/v1/map/links?link_kind=traceroute&since={three_days}", 250
    yield f"/api/v1/map/text-senders?since={three_days}"
    for kind in ("traceroute", "direct", "neighbor"):
        yield f"/api/v1/map/links?link_kind={kind}&since={day}"
        yield f"/api/v1/links?link_kind={kind}&since={day}"
        # Longer windows add up more hourly rows than the output limit bounds.
        # Seven days is a chosen window; the page opens on 24 hours.
        yield f"/api/v1/links?link_kind={kind}&since={week}", WEEK_LINKS_BUDGET_MS[kind]
    # The map loads every traceroute link in history at once (2 MB on live).
    yield "/api/v1/map/links?link_kind=traceroute", 650
    yield f"/api/v1/map/text-senders?since={day}"
    # The coverage layer's heard nodes, over the whole world.
    yield f"/api/v1/coverage/nodes?west=-180&south=-85&east=180&north=85&channel={channel}"
    yield "/api/v1/coverage/cells?west=-180&south=-85&east=180&north=85&zoom=6&unknown=true"
    yield "/api/v1/coverage/cells/8/143/83?unknown=true"
    yield f"/api/v1/map/text-reach/{subjects.text_sender}?since={day}"
    yield "/api/v1/chat"
    yield f"/api/v1/chat?channel={channel}"
    yield f"/api/v1/chat?channel={channel}&after_id=1"
    yield "/api/v1/chat?before_id=100"
    # A search nothing matches reads every text message.
    yield "/api/v1/chat?search=zzqx"
    yield f"/api/v1/chat?hide={subjects.text_sender}"
    yield "/api/v1/chat/channels"
    for sort in ("newest", "route", "distance"):
        yield f"/api/v1/traceroutes?sort={sort}&limit=50&offset=0&since={day}"
    yield f"/api/v1/traceroutes?packet={subjects.traceroute_packet}"
    yield (
        f"/api/v1/traceroutes?packet={subjects.traceroute_packet}&node={node}&next_to={neighbour}"
    )
    yield (
        f"/api/v1/traceroutes?sort=newest&limit=50&offset=0&since={day}"
        f"&node={name}&next_to=!{neighbour:08x}&channel={channel}&min_hops=1&successful=true"
    )
    # A busy relay over all history ranks every run it is on.
    yield f"/api/v1/traceroutes?sort=route&limit=50&offset=0&node=!{node:08x}", 500
    yield f"/api/v1/traceroutes/hops?node_a={node}&node_b={neighbour}&since={day}&channel={channel}"
    # Line of sight from a Links row, where the pair was when last heard.
    at = now.isoformat().replace("+00:00", "Z")
    yield f"/api/v1/links/positions?node_a={node}&node_b={neighbour}&at={at}"
    # A Links row's hourly strip, over the longest window with a start.
    yield (
        f"/api/v1/links/hours?node_a={min(node, neighbour)}&node_b={max(node, neighbour)}"
        f"&channel={channel}&since={week}"
    )
    yield f"/api/v1/traceroutes/busiest?since={day}"
    # Counts every hop row of the week; the explorer opens on 24 hours.
    yield f"/api/v1/traceroutes/busiest?since={week}", 300
    # Zones runs its graph analysis on every request.
    yield "/api/v1/zones?window_hours=72&per_day=2&min_margin_db=4", 300
    yield "/api/v1/diagnostics/summary"
    yield "/api/v1/diagnostics/doubtful-nodes"
    yield "/api/v1/diagnostics/device-conflicts?after_node=0&count=30"
    yield f"/api/v1/diagnostics/device-conflicts?after_node={node}&count=30"
    yield "/api/v1/diagnostics/longest-links?offset=0&count=30"
    yield "/api/v1/diagnostics/longest-links?offset=0&count=30&order=latest"
    # Looking further down builds the evidence of 300 links.
    yield "/api/v1/diagnostics/longest-links?offset=30&count=300&only=unlikely", 250
    yield f"/api/v1/diagnostics/longest-links?offset=0&count=300&node={node}"
    # An operator page over 25 traceroutes
    # (perf.db 2026-10-27: 0.9 s warm, 2.5 s cold).
    yield "/api/v1/diagnostics/relay-checks?count=25", 1500
    yield f"/api/v1/diagnostics/relay-checks/{subjects.relayed_reception}"
    yield f"/api/v1/diagnostics/relay-checks/{subjects.relayed_reception}?receiver=gateway"
    # The same copy as if heard by a node with no direct links elsewhere.
    yield (
        f"/api/v1/diagnostics/relay-checks/{subjects.relayed_reception}"
        "?latitude=52.0&longitude=19.0"
    )
    # Survey uploads beside them (the sample has none: tests/test_survey_checks.py).
    yield "/api/v1/diagnostics/survey-checks?count=25"
