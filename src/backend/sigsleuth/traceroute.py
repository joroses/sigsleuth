"""Turn one gateway's report of a traceroute into routes and hops.

Node A sends a request to node B; each relay adds itself to the request's
route and the SNR at which it heard the previous node. B adds the SNR of the
last hop and sends a reply to A, which carries the finished route towards B
while relays fill its return route. The reply is the packet whose request_id is
set. The words follow the firmware's RouteDiscovery message, whose fields are
route and route_back.

A route is one direction of one gateway's report: a request's route, or a
reply's route towards B or its return route to A. Its path is the nodes it passed and the
SNR at each, by one rule that serves capture and the packet page:

    path = [origin] + relays + [destination]
    hop i = path[i] -> path[i + 1], measured at path[i + 1] with snr[i]

A request's path stops at its last relay: a few gateways upload a request with
an SNR but without adding themselves, which would read as a hop into the
target. The reply carries that hop instead.

A relay that could not add itself leaves a gap (None), and hops next to a gap
are skipped: the nodes on either side of it never become a hop. A step at
exactly 0 dB is skipped too (MQTT_HOP_SNR_DB): it usually crossed an MQTT
bridge, not the air. Routes can omit relays on older firmware, so hops are
"reported by traceroutes", not proven radio contact.

A traceroute's longest route is the one with the most km along its hops, then
the most hops; only hops whose two nodes have positions count toward km, so it
is approximate.

A successful route is one known to have reached its end (the rule is
reached_end). Other gateways hear a packet on its way, so their routes may stop
short of it.

A traceroute's route that worked is the route the target answered: the forward
route of its earliest reply, which every gateway reports the same, or without
a saved reply the request's route as the target itself reported it (one hop
short, see above). It is chosen over a longer route, which is usually a flood
copy that did not matter. A target answers only the first copy it hears, but a
late copy can get a second reply after the target has forgotten the first; the
earliest answer is kept.

Every reader of saved reports (capture's traceroute summary, the explorer's
node search and the packet page) goes through read_traceroute_report and
report_routes, so these rules live here and nowhere else; the browser only
groups, orders and draws what the API sends.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace

from sigsleuth.decode import UNKNOWN_NODE, RouteDiscovery, read_route_discovery
from sigsleuth.radio import great_circle_km

UNKNOWN_SNR = -128  # quarter decibels, i.e. -32 dB
MAX_SNR_DB = 30.0
# A node that received the traceroute from MQTT downlink adds itself with the
# SNR of a packet that never crossed the air: exactly 0 dB. Such a step is
# not a hop, so an MQTT bridge never becomes a link. Real 0 dB readings are
# lost with it, about half of all 0 dB hops. Revisit if
# firmware starts marking MQTT steps (it uses -128, UNKNOWN_SNR, only for
# missing relays).
MQTT_HOP_SNR_DB = 0.0


@dataclass(frozen=True)
class RoutePath:
    """A route's path, from origin to destination.

    nodes includes the origin and destination; None marks a gap. snr_db[i] is
    the SNR at nodes[i + 1] when it heard nodes[i], None when unknown. snr_db is
    shorter than the path while the packet is still travelling.
    """

    nodes: tuple[int | None, ...]
    snr_db: tuple[float | None, ...]


@dataclass(frozen=True)
class ReportPaths:
    """The paths of one report's routes: forward is the route from A to B;
    return_path is a reply's return route from B to A, None on a request."""

    forward: RoutePath
    return_path: RoutePath | None


@dataclass(frozen=True)
class Hop:
    """from_node_id transmitted and to_node_id heard it, at snr_db when known."""

    from_node_id: int
    to_node_id: int
    snr_db: float | None


def report_paths(
    discovery: RouteDiscovery, sender: int, recipient: int, request_id: int | None
) -> ReportPaths:
    """The paths one report describes, using the packet's own sender and recipient."""
    requester, target = requester_and_target(sender, recipient, request_id)
    if request_id is None:
        snr_to_last_relay = discovery.snr_towards[: len(discovery.route)]
        return ReportPaths(_path(requester, discovery.route, target, snr_to_last_relay), None)
    return ReportPaths(
        _path(requester, discovery.route, target, discovery.snr_towards),
        _path(target, discovery.route_back, requester, discovery.snr_back),
    )


def requester_and_target(sender: int, recipient: int, request_id: int | None) -> tuple[int, int]:
    """Node A, who ran the traceroute, and node B, whom it traced: a request goes
    from A to B, and a reply swaps them."""
    if request_id is None:
        return sender, recipient
    return recipient, sender


@dataclass(frozen=True)
class TracerouteReport:
    """One gateway's report of a traceroute packet: its paths, who ran the
    traceroute (requester) and whom it traced (target), and the reporting
    gateway, which decides whether a route reached its end (reached_end). gateway
    may be None when it is known to be neither end."""

    paths: ReportPaths
    requester: int
    target: int
    gateway: int | None

    @property
    def is_reply(self) -> bool:
        return self.paths.return_path is not None

    @property
    def node_ids(self) -> set[int]:
        """Every known node on the report's paths."""
        return {
            node
            for path in (self.paths.forward, self.paths.return_path)
            if path is not None
            for node in path.nodes
            if node is not None
        }


def read_traceroute_report(
    route_payload: bytes | None,
    sender: int,
    recipient: int,
    request_id: int | None,
    gateway: int | None,
) -> TracerouteReport | None:
    """A saved RouteDiscovery payload as the report it is, using the packet's
    own sender, recipient and request_id; None when it is unreadable."""
    discovery = None if route_payload is None else read_route_discovery(bytes(route_payload))
    if discovery is None:
        return None
    requester, target = requester_and_target(sender, recipient, request_id)
    return TracerouteReport(
        report_paths(discovery, sender, recipient, request_id), requester, target, gateway
    )


def without_nodes(report: TracerouteReport, hidden: Collection[int]) -> TracerouteReport:
    """The report with hidden nodes as gaps, as relays that could not add
    themselves, so no hop names them."""
    if not hidden:
        return report

    def path(found: RoutePath) -> RoutePath:
        return replace(found, nodes=tuple(None if n in hidden else n for n in found.nodes))

    paths = report.paths
    return replace(
        report,
        paths=ReportPaths(
            path(paths.forward), None if paths.return_path is None else path(paths.return_path)
        ),
    )


def report_hops(paths: ReportPaths) -> list[Hop]:
    """The measured hops in both of a report's paths, skipping gaps and hops
    from a node to itself."""
    return [
        hop
        for path in (paths.forward, paths.return_path)
        if path is not None
        for hop in path_hops(path)
    ]


def path_hops(path: RoutePath) -> list[Hop]:
    """The measured hops in one path, by the rule of report_hops: none next to
    a gap, and none at exactly 0 dB, which likely crossed MQTT."""
    hops = []
    for index, snr_db in enumerate(path.snr_db):
        from_node, to_node = path.nodes[index], path.nodes[index + 1]
        if (
            from_node is not None
            and to_node is not None
            and from_node != to_node
            and snr_db != MQTT_HOP_SNR_DB
        ):
            hops.append(Hop(from_node, to_node, snr_db))
    return hops


@dataclass(frozen=True)
class Route:
    """One route of a report: is_return is a reply's return route to the requester.
    hops counts the steps it made (those with an SNR slot); km adds up those
    whose two nodes have positions, None when none do. reached is whether it is
    known to have reached its end (reached_end)."""

    path: RoutePath
    is_return: bool
    hops: int
    km: float | None
    reached: bool


def report_routes(
    report: TracerouteReport, positions: Mapping[int, tuple[float, float]]
) -> list[Route]:
    """Every route of one report: its forward route, then a reply's return route.
    positions are the nodes' latitude and longitude, for km."""
    routes = [_route(report, False, positions)]
    if report.is_reply:
        routes.append(_route(report, True, positions))
    return routes


def successful_route(routes: list[Route]) -> Route | None:
    """The report's route that worked (see above): its forward route when that
    reached its end, None otherwise. routes are report_routes'."""
    return routes[0] if routes[0].reached else None


def reached_end(report: TracerouteReport, is_return: bool) -> bool:
    """Whether the report's forward route, or with is_return a reply's return
    route, is known to have reached its end. A reply proves that the request
    reached the target, so a reply's forward route did; its return route did when
    the requester's own gateway reported it, and a request's route when the
    target's own gateway did. A complete-looking route from another gateway
    does not count."""
    if is_return:
        return report.gateway == report.requester
    return report.is_reply or report.gateway == report.target


def hop_between(path: RoutePath, first: Collection[int], second: Collection[int]) -> Hop | None:
    """The path's first measured hop, by the rule of report_hops, between a node
    of first and a node of second, either way; None without one."""
    for hop in path_hops(path):
        if (hop.from_node_id in first and hop.to_node_id in second) or (
            hop.from_node_id in second and hop.to_node_id in first
        ):
            return hop
    return None


def route_rank(route: Route) -> tuple[float, int]:
    """Sort key of the longest-route rule: km first, then hops."""
    return (route.km or 0.0, route.hops)


def _route(
    report: TracerouteReport, is_return: bool, positions: Mapping[int, tuple[float, float]]
) -> Route:
    path = (
        report.paths.return_path if is_return and report.paths.return_path else report.paths.forward
    )
    km = None
    for hop in path_hops(path):
        a, b = hop.from_node_id, hop.to_node_id
        if a in positions and b in positions:
            km = (km or 0.0) + great_circle_km(*positions[a], *positions[b])
    return Route(path, is_return, len(path.snr_db), km, reached_end(report, is_return))


def _path(
    origin: int, relays: tuple[int, ...], destination: int, snr: tuple[int, ...]
) -> RoutePath:
    nodes = tuple(None if node == UNKNOWN_NODE else node for node in (origin, *relays, destination))
    return RoutePath(nodes, tuple(_snr_db(value) for value in snr[: len(nodes) - 1]))


def _snr_db(quarter_db: int) -> float | None:
    """Quarter decibels to dB. 0 dB stays a reading here (path_hops skips it);
    the header's 0 is not one."""
    snr_db = quarter_db / 4
    if quarter_db == UNKNOWN_SNR or abs(snr_db) > MAX_SNR_DB:
        return None
    return snr_db
