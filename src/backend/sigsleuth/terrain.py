"""Ground height from terrain tiles, and the terrain check on long links.

Most links heard 250 km apart or more are wrong positions, not radio: ground
radio reaches that far only from high above the land between, or over sea.
So a link of LONG_LINK_KM or more is unlikely unless one end stands
ABOVE_LAND_M or more above the median ground between the ends (sea counted
as 0 m), or at least SEA_SHARE of the path is sea. Whatever the heights, a
link crossing more than MAX_LAND_KM of land is unlikely: a wrong position can
land on high ground and lift its own link.

The check is there to catch wrong positions, so a pair whose ends are both
confirmed in place passes whatever the terrain says: each end was heard
directly within CONFIRMING_PARTNER_KM of a partner, or heard one, and neither
is doubtful nor has moved.

refresh_long_links judges each node pair with a link hour of LONG_LINK_KM or
more once, at its longest hour's positions, and keeps the verdict in
long_link (derived.sql); capture runs it on a thread of its own about every
five minutes, and the API at startup and after a rebuild. Ground heights come
from the AWS open-data Terrarium tiles that line of sight also uses, fetched
when a pair is judged and kept only in memory for that refresh. A pair whose
tiles cannot be fetched is left unjudged, shown as before, and tried again at
the next refresh.

Pages treat an unlikely pair's long links like a doubtful node's
(links.unlikely_links).
"""

import concurrent.futures
import dataclasses
import io
import json
import logging
import math
import sqlite3
import statistics
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from PIL import Image

from sigsleuth.db import transaction
from sigsleuth.links import LONG_LINK_KM
from sigsleuth.radio import EARTH_RADIUS_KM, great_circle_km
from sigsleuth.reported import positions_when_heard

LOGGER = logging.getLogger(__name__)

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_SIZE = 256
# Judging every long pair from nothing takes hundreds of tiles, a later
# refresh a dozen or so. Capture does not wait for any of this (mqtt.py), so a
# slow or hanging tile server only delays verdicts.
FETCH_TIMEOUT_SECONDS = 20
FETCH_PARALLEL = 6
# Path heights come from zoom 9 tiles (about 200 m a pixel in Poland) every
# PATH_STEP_KM; end heights from zoom 12 (about 25 m).
PATH_ZOOM = 9
END_ZOOM = 12
PATH_STEP_KM = 1.0

# Thresholds from the labelled long links of live loranet.pl, 10-11 October
# 2026. LONG_LINK_KM, the length from which a link is checked, is in links.py.
ABOVE_LAND_M = 300
# A path at least this much over sea passes without ABOVE_LAND_M.
SEA_SHARE = 0.5
# 2026-10-13: no real link on live loranet.pl crossed over 400 km of land. A
# swapped latitude and longitude can put one end on high ground thousands of
# km away, high enough above the land between to pass on height.
MAX_LAND_KM = 600
# Ground below this is sea: the tiles carry sea-floor depth there. Polders
# such as Żuławy reach about -2 m.
SEA_M = -2.0
# An end's reported altitude counts unless it is more than this above the
# highest ground within END_RADIUS_KM of its position; then the ground does.
# That rejects junk altitudes while keeping masts.
END_ALTITUDE_SLACK_M = 200
END_RADIUS_KM = 0.3
END_STEP_KM = 0.1
# Without the moved and doubtful exclusions, GPS-jump pairs came back: their
# ends also have a near partner.
CONFIRMING_PARTNER_KM = 60

TileKey = tuple[int, int, int]


class TerrainUnavailable(Exception):
    """A tile needed for a height could not be fetched or read."""


def fetch_tile(key: TileKey) -> bytes:
    """One Terrarium tile's PNG bytes."""
    z, x, y = key
    with urllib.request.urlopen(
        TILE_URL.format(z=z, x=x, y=y), timeout=FETCH_TIMEOUT_SECONDS
    ) as response:
        return bytes(response.read())


def _tile_xy(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    """Web Mercator tile coordinates; the integer parts name the tile."""
    n = 2**zoom
    sin_lat = math.sin(math.radians(latitude))
    x = (longitude + 180) / 360 * n
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * n
    return x, y


class Terrain:
    """Ground heights from tiles fetched on first use and kept while this
    object lives: one refresh. A tile that failed stays failed. Once stopping()
    is true no new tile is fetched, so shutdown waits at most one
    FETCH_TIMEOUT_SECONDS for the requests already running, not for the queue
    (Python joins the fetch threads at exit)."""

    def __init__(self, stopping: Callable[[], bool] = lambda: False) -> None:
        self._tiles: dict[TileKey, bytes | None] = {}
        self._stopping = stopping

    def prefetch(self, points: Iterable[tuple[float, float]], zoom: int) -> None:
        """Fetch, a few at once, the tiles under points not fetched yet."""
        keys = {
            (zoom, int(x), int(y)) for x, y in (_tile_xy(lat, lon, zoom) for lat, lon in points)
        } - self._tiles.keys()
        with concurrent.futures.ThreadPoolExecutor(FETCH_PARALLEL) as pool:
            for key, pixels in zip(sorted(keys), pool.map(self._load, sorted(keys)), strict=True):
                self._tiles[key] = pixels

    def height(self, latitude: float, longitude: float, zoom: int) -> float:
        """Ground height in metres; below SEA_M at sea."""
        x, y = _tile_xy(latitude, longitude, zoom)
        key = (zoom, int(x), int(y))
        if key not in self._tiles:
            self._tiles[key] = self._load(key)
        pixels = self._tiles[key]
        if pixels is None:
            raise TerrainUnavailable(key)
        row = min(TILE_SIZE - 1, int(y % 1 * TILE_SIZE))
        column = min(TILE_SIZE - 1, int(x % 1 * TILE_SIZE))
        offset = (row * TILE_SIZE + column) * 3
        red, green, blue = pixels[offset : offset + 3]
        return red * 256 + green + blue / 256 - 32768

    def _load(self, key: TileKey) -> bytes | None:
        """A tile's RGB pixels, row by row; None when it could not be loaded
        or the refresh is stopping."""
        if self._stopping():
            return None
        try:
            data = fetch_tile(key)
            return Image.open(io.BytesIO(data)).convert("RGB").tobytes()
        except (OSError, ValueError) as error:
            LOGGER.debug("Terrain tile %s unavailable: %s", key, error)
            return None


def path_points(
    lat1: float, lon1: float, lat2: float, lon2: float, step_km: float = PATH_STEP_KM
) -> list[tuple[float, float]]:
    """Points along the great circle from one end to the other, both ends
    included, about every step_km."""
    phi1, lambda1, phi2, lambda2 = map(math.radians, (lat1, lon1, lat2, lon2))
    angle = 2 * math.asin(
        math.sqrt(
            math.sin((phi2 - phi1) / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin((lambda2 - lambda1) / 2) ** 2
        )
    )
    steps = max(2, int(angle * EARTH_RADIUS_KM / step_km))
    if angle == 0:
        return [(lat1, lon1)] * (steps + 1)
    points = []
    for k in range(steps + 1):
        a = math.sin((1 - k / steps) * angle) / math.sin(angle)
        b = math.sin(k / steps * angle) / math.sin(angle)
        x = a * math.cos(phi1) * math.cos(lambda1) + b * math.cos(phi2) * math.cos(lambda2)
        y = a * math.cos(phi1) * math.sin(lambda1) + b * math.cos(phi2) * math.sin(lambda2)
        z = a * math.sin(phi1) + b * math.sin(phi2)
        points.append(
            (math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x)))
        )
    return points


def nearby_points(
    latitude: float,
    longitude: float,
    radius_km: float = END_RADIUS_KM,
    step_km: float = END_STEP_KM,
) -> list[tuple[float, float]]:
    """A grid of points every step_km within radius_km of a position."""
    n = int(radius_km // step_km)
    return [
        (
            latitude + i * step_km / 111.2,
            longitude + j * step_km / (111.2 * math.cos(math.radians(latitude))),
        )
        for i in range(-n, n + 1)
        for j in range(-n, n + 1)
        if (i * i + j * j) * step_km**2 <= radius_km**2
    ]


End = tuple[float, float, int | None]
"""One end of a link: latitude, longitude and reported altitude in metres."""


@dataclass(frozen=True)
class LongLinkCheck:
    """The terrain check of a link: sea_share is the share of the path over
    sea (the rest, land, may be at most MAX_LAND_KM), above_meters how far
    the higher end stands above the median ground between (sea as 0 m),
    confirmed whether a near direct partner confirms both ends' positions, and
    unlikely whether none of them lets the link through."""

    sea_share: float
    above_meters: float
    confirmed: bool
    unlikely: bool


def check_long_link(a: End, b: End, terrain: Terrain, confirmed: bool = False) -> LongLinkCheck:
    """The terrain check between two ends, confirmed in place or not. Raises
    TerrainUnavailable."""
    heights = [
        terrain.height(lat, lon, PATH_ZOOM) for lat, lon in path_points(a[0], a[1], b[0], b[1])
    ]
    between = heights[1:-1] or heights
    sea_share = sum(height < SEA_M for height in between) / len(between)
    ground = statistics.median(max(height, 0.0) for height in between)
    above = max(_end_height(end, terrain) for end in (a, b)) - ground
    return LongLinkCheck(
        sea_share=round(sea_share, 2),
        above_meters=round(above),
        confirmed=confirmed,
        unlikely=not confirmed
        and (
            (sea_share < SEA_SHARE and above < ABOVE_LAND_M)
            or great_circle_km(a[0], a[1], b[0], b[1]) * (1 - sea_share) > MAX_LAND_KM
        ),
    )


def _end_height(end: End, terrain: Terrain) -> float:
    """The end's height above sea level: its reported altitude, unless that
    is more than END_ALTITUDE_SLACK_M above the highest ground near it."""
    latitude, longitude, altitude = end
    ground = terrain.height(latitude, longitude, END_ZOOM)
    if altitude is None:
        return ground
    highest = max(
        terrain.height(lat, lon, END_ZOOM) for lat, lon in nearby_points(latitude, longitude)
    )
    return max(ground, altitude) if altitude <= highest + END_ALTITUDE_SLACK_M else ground


def _prefetch(terrain: Terrain, pairs: Sequence[tuple[End, End]]) -> None:
    terrain.prefetch(
        (point for a, b in pairs for point in path_points(a[0], a[1], b[0], b[1])), PATH_ZOOM
    )
    terrain.prefetch(
        (point for pair in pairs for end in pair for point in nearby_points(end[0], end[1])),
        END_ZOOM,
    )


def refresh_long_links(
    connection: sqlite3.Connection, stopping: Callable[[], bool] = lambda: False
) -> int:
    """Judge the node pairs heard LONG_LINK_KM apart or more in saved history
    that long_link has no verdict for, at their longest hour, and drop the
    verdicts of pairs gone or with a new longest km. Returns how many pairs
    were left unjudged because their tiles could not be fetched. Gives up,
    saving nothing new, once stopping() is true."""
    longest = {
        (int(a), int(b)): (float(km), int(heard_at))
        for a, b, km, heard_at in connection.execute(
            """
            SELECT min(from_node_id, to_node_id), max(from_node_id, to_node_id),
                   max(km), last_seen_at
            FROM link_hour WHERE km >= ? GROUP BY 1, 2
            """,
            (LONG_LINK_KM,),
        )
    }
    judged = {
        (int(a), int(b)): float(km)
        for a, b, km in connection.execute("SELECT node_a, node_b, km FROM long_link")
    }
    stale = [pair for pair, km in judged.items() if longest.get(pair, (None,))[0] != km]
    if stale:
        with transaction(connection):
            connection.executemany("DELETE FROM long_link WHERE node_a = ? AND node_b = ?", stale)
    todo = {pair: found for pair, found in longest.items() if pair not in judged or pair in stale}
    if not todo:
        return 0
    ends = _ends_when_heard(connection, todo)
    confirmed = _confirmed_nodes(connection, {node for pair in ends for node in pair})
    terrain = Terrain(stopping)
    _prefetch(terrain, list(ends.values()))
    if stopping():
        return len(ends)
    rows, unjudged = [], 0
    for (a, b), (end_a, end_b) in ends.items():
        try:
            check = check_long_link(end_a, end_b, terrain, a in confirmed and b in confirmed)
        except TerrainUnavailable:
            unjudged += 1
            continue
        rows.append((a, b, todo[a, b][0], *dataclasses.astuple(check)))
    with transaction(connection):
        connection.executemany("INSERT INTO long_link VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    if unjudged:
        LOGGER.warning("Left %d long links unjudged: terrain tiles unavailable", unjudged)
    return unjudged


def _confirmed_nodes(connection: sqlite3.Connection, nodes: set[int]) -> set[int]:
    """Those of nodes heard directly within CONFIRMING_PARTNER_KM of a partner, or
    that heard one, in saved history, and neither doubtful, shared nor moved. One pass
    over the direct link hours, only when a refresh has pairs to judge."""
    return {
        int(row[0])
        for row in connection.execute(
            """
            WITH near (node_id) AS (
                SELECT from_node_id FROM link_hour WHERE link_kind = 'direct' AND km <= :km
                UNION SELECT to_node_id FROM link_hour WHERE link_kind = 'direct' AND km <= :km
            )
            SELECT node_id FROM near JOIN node_current USING (node_id)
            WHERE node_id IN (SELECT value FROM json_each(:nodes))
              AND moved_at IS NULL
              AND node_id NOT IN (SELECT node_id FROM node_doubtful)
              AND node_id NOT IN (SELECT node_id FROM node_shared)
            """,
            {"nodes": json.dumps(sorted(nodes)), "km": CONFIRMING_PARTNER_KM},
        )
    }


def _ends_when_heard(
    connection: sqlite3.Connection, pairs: dict[tuple[int, int], tuple[float, int]]
) -> dict[tuple[int, int], tuple[End, End]]:
    """Each pair's ends where they were when its longest hour was last heard
    (positions_when_heard), else where node_current places them. Pairs with
    an end placed neither way are left out."""
    heard = positions_when_heard(
        connection, {(node, heard_at) for pair, (_, heard_at) in pairs.items() for node in pair}
    )
    nodes = {node for pair in pairs for node in pair}
    current = {
        int(row[0]): (float(row[1]), float(row[2]), row[3])
        for row in connection.execute(
            """
            SELECT node_id, latitude, longitude, altitude_meters FROM node_current
            WHERE latitude IS NOT NULL AND node_id IN (SELECT value FROM json_each(?))
            """,
            (json.dumps(sorted(nodes)),),
        )
    }
    ends = {}
    for pair, (_, heard_at) in pairs.items():
        found = []
        for node in pair:
            position = heard.get((node, heard_at))
            if position is not None:
                found.append((position.latitude, position.longitude, position.altitude_meters))
            elif node in current:
                found.append(current[node])
        if len(found) == 2:
            ends[pair] = (found[0], found[1])
    return ends
