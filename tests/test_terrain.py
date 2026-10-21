import io
import math
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

from derived_rows import link_hour
from sigsleuth import terrain
from sigsleuth.db import migrate_database, open_current_database
from sigsleuth.links import unlikely_links
from sigsleuth.terrain import Terrain, check_long_link, refresh_long_links

A, B, C, D = 0x0A0A0A0A, 0x0B0B0B0B, 0x0C0C0C0C, 0x0D0D0D0D
# Two ends on the 50th parallel 3.7 degrees apart: about 264 km.
WEST, EAST = (50.0, 15.0), (50.0, 18.7)


class FakeTerrain(Terrain):
    def __init__(self, ground: Callable[[float, float], float]) -> None:
        super().__init__()
        self.ground = ground

    def height(self, latitude: float, longitude: float, zoom: int) -> float:
        return self.ground(latitude, longitude)


def test_ground_height_reads_its_pixel_of_its_tile(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each pixel encodes 10 m per column plus 1 m per row, as Terrarium does.
    image = Image.new("RGB", (256, 256))
    for row in range(256):
        for column in range(256):
            value = 10 * column + row + 32768
            image.putpixel((column, row), (value // 256, value % 256, 0))
    png = io.BytesIO()
    image.save(png, format="PNG")
    asked = []

    def fetch(key: terrain.TileKey) -> bytes:
        asked.append(key)
        return png.getvalue()

    monkeypatch.setattr(terrain, "fetch_tile", fetch)
    # At zoom 1, tile (1, 1, 0) covers 0-180 east, 0-85 north. Aim at the
    # middle of its pixel in column 128, row 64.
    longitude = (1 + 128.5 / 256) / 2 * 360 - 180
    latitude = math.degrees(math.atan(math.sinh(math.pi * (1 - 64.5 / 256))))
    height = Terrain().height(latitude, longitude, 1)

    assert asked == [(1, 1, 0)]
    assert height == 10 * 128 + 64


def test_a_long_link_over_low_land_is_unlikely() -> None:
    lowland = FakeTerrain(lambda lat, lon: 100.0)

    check = check_long_link((*WEST, 150), (*EAST, 150), lowland)

    assert check.unlikely
    assert check.above_meters == 50
    assert check.sea_share == 0


def test_a_mountain_end_makes_a_long_link_likely() -> None:
    mountain = FakeTerrain(lambda lat, lon: 1200.0 if lon < 15.05 else 100.0)

    assert not check_long_link((*WEST, None), (*EAST, 150), mountain).unlikely


def test_height_counts_from_the_land_between_not_from_sea_level() -> None:
    plateau = FakeTerrain(lambda lat, lon: 700.0)

    check = check_long_link((*WEST, 750), (*EAST, 750), plateau)

    assert check.unlikely
    assert check.above_meters == 50


def test_a_long_link_mostly_over_sea_is_likely() -> None:
    coast = FakeTerrain(lambda lat, lon: 20.0 if lon < 16.5 else -200.0)

    check = check_long_link((*WEST, 30), (*EAST, 30), coast)

    assert not check.unlikely
    assert check.sea_share > 0.5


def test_a_link_over_too_much_land_is_unlikely_whatever_the_heights() -> None:
    # A position with latitude and longitude swapped: a node from southern
    # Poland placed in Arabia on high ground, about 4,300 km of land away.
    highland = FakeTerrain(lambda lat, lon: 900.0 if lon > 45 else 500.0)

    check = check_long_link((50.10, 20.00, None), (19.94, 50.06, None), highland)

    assert check.above_meters >= 300
    assert check.unlikely


def test_an_altitude_far_above_the_ground_falls_back_to_the_ground() -> None:
    lowland = FakeTerrain(lambda lat, lon: 100.0)

    check = check_long_link((*WEST, 11_318), (*EAST, None), lowland)

    assert check.unlikely
    assert check.above_meters == 0


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "history.db"
    migrate_database(path)
    with closing(open_current_database(path)) as connection:
        yield connection


def place(connection: sqlite3.Connection, node_id: int, latitude: float, longitude: float) -> None:
    with connection:
        connection.execute(
            """
            INSERT INTO node_current (node_id, last_seen_at, position_observed_at,
                latitude, longitude, altitude_meters)
            VALUES (?, 1, 1, ?, ?, 150)
            """,
            (node_id, latitude, longitude),
        )


def heard(connection: sqlite3.Connection, a: int, b: int, km: float, hour: int = 0) -> None:
    with connection:
        link_hour(connection, a, b, hour * 3_600_000 + 1, km=km)


CHECK = terrain.LongLinkCheck(sea_share=0.0, above_meters=50, confirmed=False, unlikely=True)


def test_refresh_judges_long_pairs_once_and_again_when_their_longest_km_changes(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    place(connection, A, *WEST)
    place(connection, B, *EAST)
    place(connection, C, 50.0, 15.5)
    heard(connection, A, B, 264.0)
    heard(connection, A, C, 36.0)
    judged = []

    def check(
        a: terrain.End, b: terrain.End, land: Terrain, confirmed: bool
    ) -> terrain.LongLinkCheck:
        judged.append((a, b))
        return CHECK

    monkeypatch.setattr(terrain, "check_long_link", check)

    refresh_long_links(connection)
    refresh_long_links(connection)
    assert len(judged) == 1
    assert unlikely_links(connection) == {(A, B)}

    heard(connection, B, A, 270.0, hour=1)
    refresh_long_links(connection)
    assert len(judged) == 2
    assert [row[0] for row in connection.execute("SELECT km FROM long_link")] == [270.0]

    with connection:
        connection.execute("DELETE FROM link_hour WHERE km > 250")
    refresh_long_links(connection)
    assert unlikely_links(connection) == set()


def test_a_pair_whose_tiles_fail_is_judged_at_a_later_refresh(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    place(connection, A, *WEST)
    place(connection, B, *EAST)
    heard(connection, A, B, 264.0)

    assert refresh_long_links(connection) == 1  # conftest's tiles all fail
    assert unlikely_links(connection) == set()

    monkeypatch.setattr(terrain.Terrain, "height", lambda self, lat, lon, zoom: 100.0)
    assert refresh_long_links(connection) == 0
    assert unlikely_links(connection) == {(A, B)}


def test_a_long_link_between_ends_confirmed_by_near_direct_partners_passes(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Low land all the way, so only confirmed ends can let a pair through.
    # C is 36 km from A; D moved since its partner 5 km away heard it.
    monkeypatch.setattr(terrain.Terrain, "height", lambda self, lat, lon, zoom: 100.0)
    place(connection, A, *WEST)
    place(connection, B, *EAST)
    place(connection, C, 50.0, 15.5)
    place(connection, D, 50.0, 18.6)
    with connection:
        connection.execute("UPDATE node_current SET moved_at = 1 WHERE node_id = ?", (D,))
    heard(connection, A, B, 264.0)
    heard(connection, A, D, 257.0)
    heard(connection, C, A, 36.0)
    heard(connection, B, C, 228.0)
    heard(connection, D, 0x0F0F0F0F, 5.0)

    refresh_long_links(connection)
    assert unlikely_links(connection) == {(A, B), (A, D)}

    # Once something near hears B, a fresh verdict confirms it.
    heard(connection, 0x0E0E0E0E, B, 20.0)
    with connection:
        connection.execute("DELETE FROM long_link")
    refresh_long_links(connection)
    assert unlikely_links(connection) == {(A, D)}


def test_a_stopping_refresh_fetches_no_more_tiles(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python waits for queued fetches at exit, so stopping must empty the queue.
    place(connection, A, *WEST)
    place(connection, B, *EAST)
    heard(connection, A, B, 264.0)
    fetched = []

    def fetch(key: terrain.TileKey) -> bytes:
        fetched.append(key)
        raise OSError("shutting down")

    monkeypatch.setattr(terrain, "fetch_tile", fetch)
    monkeypatch.setattr(terrain, "FETCH_PARALLEL", 1)

    assert refresh_long_links(connection, lambda: len(fetched) > 0) == 1
    assert len(fetched) == 1
    assert connection.execute("SELECT count(*) FROM long_link").fetchone()[0] == 0
