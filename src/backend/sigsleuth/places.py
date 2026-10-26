"""Towns and cities with more than 15,000 people, for naming zones and
chatty senders' regions.

The list is cities.tsv.gz, cut from GeoNames (CC BY 4.0) by scripts/cities.py.
It is read on first use, about 32,000 places, and kept for the process.
Population lets zones take the biggest city inside their shaded area.
"""

from __future__ import annotations

import gzip
import math
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from sigsleuth.radio import great_circle_km

CITIES = Path(__file__).with_name("cities.tsv.gz")


@dataclass(frozen=True)
class City:
    name: str
    region: str | None
    latitude: float
    longitude: float
    population: int


def cities_near(latitude: float, longitude: float, km: float) -> Iterator[City]:
    """The places within km of a point; km must be under about 100. Near the
    poles, where a degree of longitude is short, some are missed."""
    grid = _grid()
    row, column = math.floor(latitude), math.floor(longitude)
    for cell in [(row + dy, column + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]:
        for city in grid.get(cell, ()):
            if great_circle_km(latitude, longitude, city.latitude, city.longitude) <= km:
                yield city


def cities_in_area(hull: tuple[tuple[float, float], ...]) -> Iterator[City]:
    """Places inside or on a convex boundary, counter-clockwise as (longitude, latitude)."""
    if len(hull) < 3:
        return
    west, east = min(p[0] for p in hull), max(p[0] for p in hull)
    south, north = min(p[1] for p in hull), max(p[1] for p in hull)
    for (row, column), cities in _grid().items():
        if not (
            math.floor(south) <= row <= math.floor(north)
            and math.floor(west) <= column <= math.floor(east)
        ):
            continue
        for city in cities:
            if all(
                (b[0] - a[0]) * (city.latitude - a[1]) - (b[1] - a[1]) * (city.longitude - a[0])
                >= -1e-12
                for a, b in zip(hull, (*hull[1:], hull[0]), strict=True)
            ):
                yield city


def nearest_city(latitude: float, longitude: float, km: float) -> City | None:
    """The place nearest a point, if one is within km (under about 100)."""
    return min(
        cities_near(latitude, longitude, km),
        key=lambda city: great_circle_km(latitude, longitude, city.latitude, city.longitude),
        default=None,
    )


@cache
def _grid() -> dict[tuple[int, int], list[City]]:
    """Every place, by the whole degrees of its latitude and longitude."""
    grid: dict[tuple[int, int], list[City]] = {}
    with gzip.open(CITIES, "rt", encoding="utf-8") as file:
        for line in file:
            name, region, latitude, longitude, population = line.rstrip("\n").split("\t")
            city = City(name, region or None, float(latitude), float(longitude), int(population))
            grid.setdefault((math.floor(city.latitude), math.floor(city.longitude)), []).append(
                city
            )
    return grid
