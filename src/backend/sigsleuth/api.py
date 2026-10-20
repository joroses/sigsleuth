"""Versioned HTTP API for the SigSleuth browser: its routes and errors.
Each page's read queries and response classes are in its own module (packets.py,
nodes.py, map_nodes.py, chat.py, links.py, traceroute_explorer.py, zones.py,
diagnostics.py, coverage.py). The one route that writes takes signal survey
uploads from phones (coverage.py).

At startup the app starts its background work (maintenance.py: rebuilding
derived tables when they wait for it, retention, estimates) and, with
capture=True, MQTT capture, each on a thread of its own, and stops them at
shutdown, so one process serves and records history.

The running app lists every endpoint and response shape at /docs. The response
classes are also the frontend's types: after changing one, run
scripts/api-types.sh. Conventions:

- Times are UTC server receipt times unless a field is called device_time.
- Lists are newest first and bounded by limit. A page with more items returns
  next_before_id; pass it back as before_id for the next page. Chat is the
  exception: its messages read oldest first, like a conversation.
- A …Page class is one paginated slice; a …List class is a whole bounded
  list (its truncated, when present, says the limit cut it).
- Errors are {error: {code, message}}: 400 bad parameters or body, 404 unknown
  item, 503 database unavailable or busy. Missing grouping or node rows never fail a request.
"""

import base64
import binascii
import hmac
import html
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi import Path as ApiPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from sigsleuth import links, traceroute_explorer
from sigsleuth.chat import ChatChannelList, ChatPage, chat_channels, chat_messages
from sigsleuth.config import Settings
from sigsleuth.coverage import (
    FINE_LEVEL,
    MIN_CELL_LEVEL,
    CoverageCellDetail,
    CoverageCellList,
    CoverageExtent,
    Upload,
    UploadResult,
    coverage_cell,
    coverage_cells,
    coverage_extent,
    save_upload,
)
from sigsleuth.db import (
    DatabaseVersionError,
    open_current_database,
    rederive_pending,
    utc_now,
)
from sigsleuth.diagnostics import (
    LONGEST_LINKS,
    MAX_LONG_LINK_SCAN,
    DiagnosticsSummary,
    DoubtfulNodeList,
    LongLinkFilter,
    LongLinkList,
    LongLinkOrder,
    diagnostics_summary,
    doubtful_node_list,
    longest_links,
)
from sigsleuth.links import LinkKind, MapLinkList, map_links
from sigsleuth.maintenance import Maintenance
from sigsleuth.map_nodes import (
    MapNodeList,
    TextReach,
    TextSenderList,
    map_nodes,
    text_reach,
    text_senders,
)
from sigsleuth.mqtt import CaptureStatus, MqttReceiver
from sigsleuth.nodes import (
    NODE_CHART_DAYS,
    NodeChannelList,
    NodeCharts,
    NodeContactList,
    NodeListPage,
    NodeOverview,
    NodePositionHistory,
    NodeSort,
    node_channels,
    node_charts,
    node_contacts,
    node_list,
    node_overview,
    node_packets,
    node_position_history,
)
from sigsleuth.packets import (
    PacketDetail,
    PacketPage,
    PacketPortList,
    packet_detail,
    packet_list,
    packet_ports,
)
from sigsleuth.positions import HeardPositionList, positions_at
from sigsleuth.retention import coverage_limit_days
from sigsleuth.zones import NetworkZones, network_zones

LOGGER = logging.getLogger(__name__)

MAX_PAGE_SIZE = 200
# Live has a few thousand placed map nodes; revisit viewport requests if
# normal captures regularly reach this cap.
MAX_MAP_ITEMS = 10_000
# Seven days of live loranet.pl held about 7,300 traceroute links (2026-10-07).
MAX_MAP_LINKS = 20_000
ZONE_WINDOW_HOURS = (24, 72, 168)
MAX_TEXT_SENDERS = 100


@dataclass(frozen=True)
class Status:
    """capture is null when the app was started without capture."""

    capture: CaptureStatus | None
    status: Literal["ok"] = "ok"


class ApiError(Exception):
    """An expected request failure returned as {error: {code, message}}."""

    def __init__(
        self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def create_app(
    settings: Settings,
    frontend_path: str | Path | None = None,
    *,
    capture: bool = False,
) -> FastAPI:
    """Create the API; with `capture`, it also records MQTT deliveries while running."""

    @asynccontextmanager
    async def run_background_work(app: FastAPI) -> AsyncIterator[None]:
        maintenance = Maintenance(settings, retain=capture)
        maintenance.start()
        try:
            if not capture:
                yield
                return
            with closing(open_current_database(settings.database_path)) as connection:
                receiver = MqttReceiver(settings, connection, maintenance)
                app.state.receiver = receiver
                receiver.start()
                try:
                    yield
                finally:
                    receiver.stop()
        finally:
            maintenance.stop()

    app = FastAPI(title="SigSleuth API", version="1", lifespan=run_background_work)
    app.state.receiver = None

    @app.middleware("http")
    async def record_map_timing(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started_at = time.perf_counter()
        response = await call_next(request)
        query_ms = getattr(request.state, "map_query_ms", None)
        if query_ms is not None:
            total_ms = (time.perf_counter() - started_at) * 1000
            serialization_ms = max(0.0, total_ms - query_ms)
            response.headers["Server-Timing"] = (
                f"query;dur={query_ms:.1f}, serialization;dur={serialization_ms:.1f}"
            )
        return response

    app.state.api_started_at = utc_now()
    app.state.frontend_path = (
        Path(frontend_path)
        if frontend_path is not None
        else Path(__file__).resolve().parents[3] / "src" / "frontend" / "dist"
    )

    def read_connection() -> Iterator[sqlite3.Connection]:
        connection = open_current_database(settings.database_path)
        try:
            yield connection
        finally:
            connection.close()

    Connection = Annotated[sqlite3.Connection, Depends(read_connection)]
    PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
    BeforeId = Annotated[int | None, Query(gt=0)]

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return _error_response(400, "invalid_parameters", "The request parameters are invalid.")

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError) -> JSONResponse:
        return _error_response(error.status_code, error.code, error.message, error.headers)

    @app.exception_handler(DatabaseVersionError)
    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def database_unavailable(request: Request, error: Exception) -> JSONResponse:
        LOGGER.error("Database unavailable for %s", request.url.path, exc_info=error)
        return _error_response(
            503,
            "database_unavailable",
            "SigSleuth history is temporarily unavailable.",
        )

    def capture_status() -> CaptureStatus | None:
        receiver: MqttReceiver | None = app.state.receiver
        return None if receiver is None else receiver.status()

    def admin_only(request: Request) -> None:
        """Diagnostics takes the admin password as HTTP Basic, with any user
        name. Browsers ask for it."""
        password = settings.admin_password
        if password is not None and not _has_password(request, password):
            raise _password_required("admin")

    def uploaders_only(request: Request) -> None:
        """Uploads take the upload password the same way; the survey app sends
        it. Without one, uploads are refused."""
        password = settings.upload_password
        if password is None:
            raise ApiError(403, "uploads_disabled", "Survey uploads need an upload password.")
        if not _has_password(request, password):
            raise _password_required("upload")

    Admin = Depends(admin_only)
    Uploader = Depends(uploaders_only)

    @app.get("/api/v1/status")
    def status() -> Status:
        return Status(capture=capture_status())

    @app.get("/api/v1/packets")
    def packet_list_page(
        connection: Connection,
        limit: PageLimit = 50,
        before_id: BeforeId = None,
        port: Annotated[int | None, Query(ge=-1, le=0xFFFFFFFF)] = None,
        sender: Annotated[int | None, Query(ge=1, le=0xFFFFFFFF)] = None,
        heard_directly_by: Annotated[int | None, Query(ge=1, le=0xFFFFFFFF)] = None,
    ) -> PacketPage:
        """Saved packets, newest first, only those on application port number
        `port` when given (-1 is packets with no readable report), those
        `sender` sent, and those gateway `heard_directly_by` heard with no
        relay. heard_directly_by needs sender."""
        if heard_directly_by is not None and sender is None:
            raise ApiError(400, "sender_required", "heard_directly_by needs sender.")
        return packet_list(
            connection,
            limit=limit,
            before_id=before_id,
            portnum=port,
            sender=sender,
            heard_directly_by=heard_directly_by,
        )

    @app.get("/api/v1/packets/ports")
    def packet_port_list(connection: Connection) -> PacketPortList:
        """Every port of a saved packet, by name, for the port filter."""
        return packet_ports(connection)

    @app.get("/api/v1/packets/{packet_row_id}")
    def packet_detail_page(
        connection: Connection,
        packet_row_id: Annotated[int, ApiPath(gt=0)],
    ) -> PacketDetail:
        """One packet's identity and payload forms, plus its receptions: heard
        directly first, then by hops used, then hops not reported. Fewer receptions
        than reception_count means the list was cut at MAX_PACKET_RECEPTIONS."""
        page = packet_detail(connection, packet_row_id, default_preset=settings.default_preset)
        if page is None:
            raise ApiError(404, "packet_not_found", "Packet not found.")
        return page

    @app.get("/api/v1/nodes")
    def node_list_page(
        connection: Connection,
        q: str = "",
        channel: str | None = None,
        gateway: bool = False,
        sort: NodeSort = "traceroute",
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> NodeListPage:
        """One page of known nodes, sorted by traceroute hops in the last 24
        hours, most first; sort=packets by packets sent and sort=texts by
        broadcast text messages sent in the last 24 hours, most first;
        sort=channel_utilization and sort=air_util_tx by the latest device
        telemetry from the last 24 hours, highest first and nodes without one
        last; sort=firmware by the estimated firmware minimum, newest first,
        and sort=firmware_oldest oldest first, nodes without one last in both;
        sort=altitude by the latest position's altitude, highest first, and
        sort=altitude_lowest lowest first, nodes without one last in both;
        or sort=last_sent by the node's latest own
        packet, newest first. Nodes known only as a gateway or from traceroute
        hops have no packet of their own and sort after the others on ties and
        under sort=last_sent.

        q matches the short or long name, the hex node ID with or without "!", or
        the exact decimal node number. channel matches the node's radio channel
        (its latest packet on a channel naming its radio settings). gateway=true
        keeps nodes that have reported as a gateway.
        total counts every matching node; page with offset.
        """
        return node_list(
            connection,
            query=q,
            channel=channel,
            gateway=gateway,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v1/nodes/channels")
    def node_channel_page(connection: Connection) -> NodeChannelList:
        """Nodes' radio channels, with their node counts, most first."""
        return node_channels(connection)

    @app.get("/api/v1/nodes/{node_id}")
    def node_overview_page(
        connection: Connection,
        node_id: Annotated[int, ApiPath(ge=1, le=0xFFFFFFFF)],
    ) -> NodeOverview:
        """A node's current summary and the ports it has sent on. An unknown
        node returns node null and no ports."""
        return node_overview(connection, node_id)

    @app.get("/api/v1/nodes/{node_id}/packets")
    def node_packet_page(
        connection: Connection,
        node_id: Annotated[int, ApiPath(ge=1, le=0xFFFFFFFF)],
        limit: PageLimit = 50,
        before_id: BeforeId = None,
        port: Annotated[int | None, Query(ge=0, le=0xFFFFFFFF)] = None,
    ) -> PacketPage:
        """Packets the node sent or that were addressed to it, only those on
        application port number `port` when given."""
        return node_packets(connection, node_id, limit=limit, before_id=before_id, portnum=port)

    @app.get("/api/v1/nodes/{node_id}/charts")
    def node_charts_page(
        connection: Connection,
        node_id: Annotated[int, ApiPath(ge=1, le=0xFFFFFFFF)],
        since: datetime | None = None,
    ) -> NodeCharts:
        """What the node sent since `since` (default the last 7 days), oldest
        first, for its charts: packets per hour and port, from the whole hour
        containing `since`, and the ChUtil and AirUtil its own device
        telemetry reported (the newest 2,000 telemetry packets at most)."""
        start = _utc(since) or utc_now() - timedelta(days=NODE_CHART_DAYS)
        return node_charts(connection, node_id, since=start)

    @app.get("/api/v1/nodes/{node_id}/positions")
    def node_position_page(
        connection: Connection,
        node_id: Annotated[int, ApiPath(ge=1, le=0xFFFFFFFF)],
    ) -> NodePositionHistory:
        """The positions the node's own newest position packets reported,
        newest first, for its position history minimap."""
        return node_position_history(connection, node_id)

    @app.get("/api/v1/nodes/{node_id}/contacts")
    def node_contact_page(
        connection: Connection,
        node_id: Annotated[int, ApiPath(ge=1, le=0xFFFFFFFF)],
        since: datetime | None = None,
    ) -> NodeContactList:
        """Every node linked with this one since `since` (default the last 7
        days) and the gateways that heard its newest packets then, with no
        map rule hiding any, nearest first: for checking where it is."""
        start = _utc(since) or utc_now() - timedelta(days=NODE_CHART_DAYS)
        return node_contacts(connection, node_id, since=start)

    @app.get("/api/v1/nodes/{node_id}/links")
    def node_link_page(
        connection: Connection,
        node_id: Annotated[int, ApiPath(ge=1, le=0xFFFFFFFF)],
        since: datetime | None = None,
    ) -> links.LinkTable:
        """The node's links of every kind since `since` (default the last 7
        days), as the Links page shows them, including those with an end that
        has no position."""
        start = _utc(since) or utc_now() - timedelta(days=NODE_CHART_DAYS)
        return links.link_table(
            connection,
            link_kinds=links.LINK_KINDS,
            since=start,
            default_preset=settings.default_preset,
            node=node_id,
        )

    @app.get("/api/v1/map/nodes")
    def map_node_page(
        request: Request,
        connection: Connection,
        seen_since: datetime | None = None,
        position_since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_MAP_ITEMS)] = MAX_MAP_ITEMS,
    ) -> MapNodeList:
        """Nodes with a latest valid position or one estimated from their
        neighbours, most recently seen first. latitude and longitude are
        where to draw it: the reported position, else the estimate.

        seen_since filters when the node was last seen (sending, reporting as a
        gateway or in a traceroute hop) and picks the links estimates come
        from; position_since filters the reported position's receipt time
        independently. truncated is true when more nodes matched.
        """
        query_start = time.perf_counter()
        page = map_nodes(
            connection,
            seen_since=_utc(seen_since),
            position_since=_utc(position_since),
            limit=limit,
        )
        request.state.map_query_ms = (time.perf_counter() - query_start) * 1000
        return page

    @app.get("/api/v1/map/links")
    def map_link_page(
        request: Request,
        connection: Connection,
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_MAP_LINKS)] = MAX_MAP_LINKS,
        link_kind: LinkKind = "traceroute",
    ) -> MapLinkList:
        """Pairs of nodes heard hearing each other, one link per pair and
        channel, most recently seen first.

        link_kind picks traceroute route steps, direct receptions (a gateway
        hearing the sender with no relay between) or neighbour info (a node's
        own list of nodes it heard with no relay); kinds are never combined.
        since starts the window at its whole hour, so it can include up to an hour
        more. truncated is true when more links matched. Hours that no longer
        match where the map draws their nodes are left out (links.TRUSTED_HOUR):
        heard before a node moved, or at a doubtful position. So are links
        that do not fit where the map draws their nodes, by reported or else
        estimated position (links.map_misfit): last heard 250 km apart or more
        on a pair the terrain check finds unlikely, drawn 250 km apart or more
        with no distance when last heard, over 150 km to an estimated node, or
        more than 25 km nearer or farther apart than when last heard.
        """
        query_start = time.perf_counter()
        start = _utc(since)
        page = map_links(
            connection,
            link_kind=link_kind,
            since=start,
            limit=limit,
            default_preset=settings.default_preset,
            trusted=True,
        )
        request.state.map_query_ms = (time.perf_counter() - query_start) * 1000
        return page

    @app.get("/api/v1/map/text-senders")
    def text_sender_page(
        connection: Connection,
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_TEXT_SENDERS)] = MAX_TEXT_SENDERS,
    ) -> TextSenderList:
        """Nodes with a valid position that sent broadcast text messages first
        received since `since`, most messages first. truncated is true when more
        senders matched."""
        return text_senders(connection, since=_utc(since), limit=limit)

    @app.get("/api/v1/map/text-reach/{node_id}")
    def text_reach_page(
        connection: Connection,
        node_id: Annotated[int, ApiPath(ge=1, le=0xFFFFFFFF)],
        since: datetime | None = None,
    ) -> TextReach:
        """The gateways that reported the node's broadcast text messages first
        received since `since`, most receptions first. Empty gateways when it
        sent none. since starts the window at its whole hour, like Chatty
        nodes, so the two count the same messages."""
        return text_reach(connection, node_id, since=_utc(since))

    @app.get("/api/v1/chat")
    def chat_page(
        connection: Connection,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
        before_id: BeforeId = None,
        after_id: Annotated[int | None, Query(ge=0)] = None,
        channel: str | None = None,
        search: Annotated[str | None, Query(max_length=100)] = None,
        hide: Annotated[list[int] | None, Query(max_length=500)] = None,
    ) -> ChatPage:
        """Readable text messages, oldest first.

        Without before_id or after_id, the newest limit messages. before_id pages
        back to older messages (next_before_id says where to continue). after_id
        returns the oldest limit messages above it, for polling; has_newer says
        more are waiting. channel matches the channel name exactly; search,
        part of the text or the sender's name. hide (repeatable) leaves out a
        sender's messages by node number.
        """
        if before_id is not None and after_id is not None:
            raise _invalid_parameters()
        return chat_messages(
            connection,
            limit=limit,
            before_id=before_id,
            after_id=after_id,
            channel=channel,
            search=search,
            hidden_senders=tuple(hide or ()),
        )

    @app.get("/api/v1/chat/channels")
    def chat_channel_page(connection: Connection) -> ChatChannelList:
        """Channels with text messages in the retained history, most messages first."""
        return chat_channels(connection)

    @app.get("/api/v1/links")
    def link_table_page(
        connection: Connection,
        link_kind: LinkKind = "traceroute",
        since: datetime | None = None,
    ) -> links.LinkTable:
        """The map's links (at most 20,000) with the distance between their
        nodes and both nodes' names, for the Links page to sort and filter."""
        return links.link_table(
            connection,
            link_kinds=(link_kind,),
            since=_utc(since),
            default_preset=settings.default_preset,
        )

    @app.get("/api/v1/links/positions")
    def link_positions_page(
        connection: Connection,
        node_a: Annotated[int, Query(ge=1, le=0xFFFFFFFF)],
        node_b: Annotated[int, Query(ge=1, le=0xFFFFFFFF)],
        at: datetime,
    ) -> HeardPositionList:
        """Where node_a and node_b were at `at` (a Links row's last_seen_at), by
        their own position packets, for line of sight. A node with none up to
        then is left out; draw it where it is now."""
        when = _utc(at)
        assert when is not None
        return positions_at(connection, (node_a, node_b), when)

    @app.get("/api/v1/links/hours")
    def link_hours_page(
        connection: Connection,
        node_a: Annotated[int, Query(ge=0, le=0xFFFFFFFF)],
        node_b: Annotated[int, Query(ge=0, le=0xFFFFFFFF)],
        channel: Annotated[str, Query(min_length=1, max_length=1024)],
        link_kind: LinkKind = "traceroute",
        since: datetime | None = None,
    ) -> links.LinkHours:
        """The hours a Links row's pair heard each other on its channel since
        `since`, each direction apart, for its hourly strip."""
        return links.link_hours(
            connection,
            link_kind=link_kind,
            node_a=node_a,
            node_b=node_b,
            channel=channel,
            since=_utc(since),
        )

    @app.get("/api/v1/traceroutes")
    def traceroute_runs_page(
        connection: Connection,
        since: datetime | None = None,
        sort: traceroute_explorer.TracerouteSort = "newest",
        node: Annotated[str | None, Query(max_length=100)] = None,
        next_to: Annotated[str | None, Query(max_length=100)] = None,
        channel: str | None = None,
        min_hops: Annotated[int, Query(ge=0, le=32)] = 0,
        successful: bool = False,
        limit: PageLimit = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> traceroute_explorer.TracerouteRuns:
        """Traceroute runs (a request and its reply) with a packet since `since`,
        newest first, by the route shown (route: km, then hops) or by the straight
        line between the ends (distance). Each run shows its route that worked,
        or without one its longest route. successful keeps only runs and routes
        known to have reached their end. node is a search (name,
        !hex or number) matching either end or a relay; with next_to, a second
        search, runs need a hop between a node of each, either way.
        Page with offset while has_more is true."""
        return traceroute_explorer.traceroute_runs(
            connection,
            since=_utc(since),
            sort=sort,
            node=node,
            next_to=next_to,
            channel=channel,
            min_hops=min_hops,
            successful=successful,
            limit=limit,
            offset=offset,
            default_preset=settings.default_preset,
        )

    @app.get("/api/v1/traceroutes/hops")
    def traceroute_hops_page(
        connection: Connection,
        node_a: Annotated[int, Query(ge=0, le=0xFFFFFFFF)],
        node_b: Annotated[int, Query(ge=0, le=0xFFFFFFFF)],
        since: datetime | None = None,
        channel: str | None = None,
    ) -> traceroute_explorer.HopObservations:
        """The newest 10 traceroute packets per direction that reported a hop
        between node_a and node_b since `since` (from its whole hour, like the
        map's links), on channel when given, as a map link is. a_to_b is node_a
        transmitting and node_b hearing it."""
        return traceroute_explorer.hop_observations(
            connection, node_a, node_b, since=_utc(since), channel=channel
        )

    @app.get("/api/v1/traceroutes/busiest")
    def busiest_traceroutes_page(
        connection: Connection, since: datetime | None = None
    ) -> traceroute_explorer.BusiestTraceroutes:
        """The ten busiest requesters, targets and relays since `since`, and
        the traceroute packets per channel."""
        return traceroute_explorer.busiest_traceroutes(connection, since=_utc(since))

    @app.get("/api/v1/zones")
    def network_zones_page(
        connection: Connection,
        window_hours: int = 72,
        channel: str | None = None,
        per_day: Annotated[float, Query(ge=0, le=100)] = 2,
        min_margin_db: Annotated[float, Query(ge=-30, le=30)] = 4,
        join_links: Annotated[int, Query(ge=1, le=4)] = 2,
    ) -> NetworkZones:
        """Zones, the links between them, key relays and text reach between
        zones, for one channel's traceroute links and broadcast texts in the
        last window_hours (at most 20,000 of the channel's links, newest
        first). Without channel, the channel with the most links. per_day and
        min_margin_db set the rule for solid links, and join_links how many
        solid links between two groups of zones join them into one;
        zones.py explains both graphs, the groups and text reach.
        window_hours is 24, 72 or 168."""
        if window_hours not in ZONE_WINDOW_HOURS:
            raise _invalid_parameters()
        return network_zones(
            connection,
            window_hours=window_hours,
            channel=channel,
            per_day=per_day,
            min_margin_db=min_margin_db,
            join_links=join_links,
            link_limit=MAX_MAP_LINKS,
            default_preset=settings.default_preset,
        )

    @app.post("/api/v1/coverage/uploads", dependencies=[Uploader])
    def coverage_upload(connection: Connection, upload: Upload) -> UploadResult:
        """Save one signal survey session's measurements from a phone. Unusable
        measurements are skipped and counted, and ones already saved are
        ignored, so a retried batch is harmless. On 503, also sent while
        derived tables are rebuilt, the phone retries the same batch later.
        Refused unless SIGSLEUTH_UPLOAD_PASSWORD is set."""
        # Upload names each relay once, partly by ruling out every other node
        # with its byte (coverage.heard_node); half-rebuilt node tables would
        # leave some out and name the wrong one for good.
        if rederive_pending(connection):
            raise ApiError(
                503, "rebuilding", "SigSleuth is rebuilding its tables; send the batch later."
            )
        return save_upload(
            connection,
            upload,
            max_age_days=coverage_limit_days(
                settings.retention_days, settings.coverage_retention_days
            ),
        )

    @app.get("/api/v1/coverage/cells")
    def coverage_cell_page(
        request: Request,
        connection: Connection,
        west: Annotated[float, Query(ge=-180, le=180)],
        south: Annotated[float, Query(ge=-90, le=90)],
        east: Annotated[float, Query(ge=-180, le=180)],
        north: Annotated[float, Query(ge=-90, le=90)],
        zoom: Annotated[float, Query(ge=0, le=24)],
        since: datetime | None = None,
        node: Annotated[int | None, Query(ge=1, le=0xFFFFFFFF)] = None,
        channel: str | None = None,
    ) -> CoverageCellList:
        """Signal survey coverage cells in the visible area, busiest first, at
        the level for the map's zoom (zoom + 6, from 8 to 17), with at most
        5,000 cells, from the measurements still true: none heard before its
        node last moved, and each either heard within
        SIGSLEUTH_RETENTION_DAYS or of a node seen since then, by MQTT or by
        a survey hearing its packets. since filters when the measurements were heard. With
        node, only the measurements where that node was the heard node,
        including its own packets heard directly. channel keeps packets whose
        sender last sent on that channel, as the map's channel filter does for
        nodes."""
        if west > east or south > north:
            raise _invalid_parameters()
        query_start = time.perf_counter()
        page = coverage_cells(
            connection,
            west=west,
            south=south,
            east=east,
            north=north,
            zoom=zoom,
            since=_utc(since),
            node=node,
            channel=channel,
            alive_days=settings.retention_days,
        )
        request.state.map_query_ms = (time.perf_counter() - query_start) * 1000
        return page

    @app.get("/api/v1/coverage/extent")
    def coverage_extent_page(
        connection: Connection,
        node: Annotated[int, Query(ge=1, le=0xFFFFFFFF)],
    ) -> CoverageExtent | None:
        """The area holding every coverage cell where survey nodes heard the
        node, from the measurements still true as in the cell list; null when
        none did."""
        return coverage_extent(connection, node, alive_days=settings.retention_days)

    @app.get("/api/v1/coverage/cells/{level}/{x}/{y}")
    def coverage_cell_detail_page(
        request: Request,
        connection: Connection,
        level: Annotated[int, ApiPath(ge=MIN_CELL_LEVEL, le=FINE_LEVEL)],
        x: Annotated[int, ApiPath(ge=0)],
        y: Annotated[int, ApiPath(ge=0)],
        since: datetime | None = None,
        node: Annotated[int | None, Query(ge=1, le=0xFFFFFFFF)] = None,
        channel: str | None = None,
    ) -> CoverageCellDetail:
        """One coverage cell's totals, the nodes heard there with how each
        was identified, unknown relay bytes and the newest 20 measurements,
        filtered by since, node and channel as in the cell list."""
        if x >= 1 << level or y >= 1 << level:
            raise _invalid_parameters()
        query_start = time.perf_counter()
        page = coverage_cell(
            connection,
            level,
            x,
            y,
            since=_utc(since),
            node=node,
            channel=channel,
            alive_days=settings.retention_days,
        )
        request.state.map_query_ms = (time.perf_counter() - query_start) * 1000
        return page

    @app.get("/api/v1/diagnostics/summary", dependencies=[Admin])
    def diagnostics_summary_page(connection: Connection) -> DiagnosticsSummary:
        return diagnostics_summary(
            connection,
            settings,
            capture=capture_status(),
            api_started_at=app.state.api_started_at,
        )

    @app.get("/api/v1/diagnostics/longest-links", dependencies=[Admin])
    def longest_links_page(
        connection: Connection,
        offset: Annotated[int, Query(ge=0)] = 0,
        count: Annotated[int, Query(ge=1, le=MAX_LONG_LINK_SCAN)] = LONGEST_LINKS,
        only: LongLinkFilter | None = None,
        node: Annotated[int | None, Query(ge=1, le=0xFFFFFFFF)] = None,
        order: LongLinkOrder = "longest",
    ) -> LongLinkList:
        """Looks through the `count` links after the `offset` first in
        `order` over all saved history: longest by distance when heard, or
        only those heard 250 km or more apart, newest such hour first. Before
        the map or Links page hides or ranks any apart, only those of `node`
        when given, and keeps those matching `only`. Each comes with its
        evidence: where its ends are, what heard it, whether the map draws it
        and warnings for a review by hand."""
        return longest_links(
            connection, offset=offset, count=count, only=only, node=node, order=order
        )

    @app.get("/api/v1/diagnostics/doubtful-nodes", dependencies=[Admin])
    def doubtful_nodes_page(connection: Connection) -> DoubtfulNodeList:
        """Nodes with a doubtful position, and nodes in two or more links the
        terrain check finds unlikely, with their long-link counts."""
        return doubtful_node_list(connection)

    @app.get("/{requested_path:path}", include_in_schema=False)
    def frontend(requested_path: str) -> Response:
        if requested_path.startswith("api/"):
            return _error_response(404, "not_found", "API route not found.")

        frontend_root = app.state.frontend_path.resolve()
        index_path = frontend_root / "index.html"
        if not index_path.is_file():
            return HTMLResponse(
                """
                <!doctype html>
                <html lang="en">
                  <head><meta charset="utf-8"><title>SigSleuth needs a web build</title></head>
                  <body>
                    <h1>SigSleuth needs a web build</h1>
                    <p>Run <code>./scripts/setup.sh</code>, then restart SigSleuth.</p>
                  </body>
                </html>
                """,
                status_code=503,
            )

        requested_file = (frontend_root / requested_path).resolve()
        if requested_file != index_path.resolve() and (
            requested_file.is_relative_to(frontend_root) and requested_file.is_file()
        ):
            return FileResponse(requested_file)
        return HTMLResponse(_named_page(index_path.read_text(encoding="utf-8"), settings.name))

    return app


def _named_page(page: str, name: str | None) -> str:
    """Put the configured instance name in the page title and a meta tag the page reads."""
    if name is None:
        return page
    escaped = html.escape(name)
    return page.replace(
        "<title>SigSleuth</title>",
        f'<title>{escaped} · SigSleuth</title><meta name="sigsleuth-name" content="{escaped}">',
        1,
    )


def _error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    content: dict[str, Any] = {"error": {"code": code, "message": message}}
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _password_required(which: str) -> ApiError:
    return ApiError(
        401,
        "password_required",
        f"This needs the {which} password.",
        {"WWW-Authenticate": 'Basic realm="SigSleuth"'},
    )


def _has_password(request: Request, password: str) -> bool:
    scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(credentials, validate=True)
    except binascii.Error:
        return False
    return hmac.compare_digest(decoded.partition(b":")[2], password.encode())


def _invalid_parameters() -> ApiError:
    return ApiError(400, "invalid_parameters", "The request parameters are invalid.")


def _utc(value: datetime | None) -> datetime | None:
    """Require an explicit offset on time parameters and convert them to UTC."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise _invalid_parameters()
    return value.astimezone(UTC)
