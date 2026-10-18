"""Write src/backend/sigsleuth/cities.tsv.gz, the towns and cities zones are named after.

Usage: uv run --locked python scripts/cities.py

It is GeoNames' list of places with more than 15,000 people
(https://download.geonames.org/export/dump/, CC BY 4.0), cut to each place's
name, region, latitude, longitude and population. Places that no longer
exist (PPLH) and districts of a city are left out. GeoNames marks few
districts as such (PPLX), so any place within SATELLITE_KM of one
SATELLITE_RATIO times bigger counts as one. Regions take their local name
rather than the English one (local_name).
It downloads about 200 MB; run it again only to pick up GeoNames' corrections.
"""

import gzip
import io
import math
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

from sigsleuth.radio import great_circle_km

DUMP = "https://download.geonames.org/export/dump"
TARGET = Path(__file__).resolve().parents[1] / "src/backend/sigsleuth/cities.tsv.gz"

# A place this close to one this many times bigger is a district or suburb of it.
SATELLITE_KM = 10
SATELLITE_RATIO = 3

# Regions whose local name GeoNames gives badly, by country and admin1 code.
LOCAL_NAMES = {"PL.85": "Warmińsko-Mazurskie", "PL.87": "Zachodniopomorskie"}


def download(name: str) -> bytes:
    with urllib.request.urlopen(f"{DUMP}/{name}", timeout=600) as response:
        body: bytes = response.read()
        return body


def local_name(code: str, english: str, names: list[list[str]], language: str) -> str:
    """The region's local name: GeoNames' preferred or short name with no
    language (which it uses for local names), else one in the country's main
    language, else English. Historic, colloquial and slang names are skipped."""
    if code in LOCAL_NAMES:
        return LOCAL_NAMES[code]
    current = [name for name in names if "1" not in name[6:9]]
    for group in (
        [name for name in current if name[2] == ""],
        [name for name in current if name[2] == language],
    ):
        for preferred, short in (("1", "1"), ("", "1"), ("1", "")):
            for name in group:
                if (not preferred or name[4] == "1") and (not short or name[5] == "1"):
                    return name[3]
    return english


def without_satellites(rows: list[str]) -> list[str]:
    """The rows (name, region, latitude, longitude, population) whose place
    has no place SATELLITE_RATIO times bigger within SATELLITE_KM."""
    places = []
    grid: dict[tuple[int, int], list[tuple[float, float, int]]] = defaultdict(list)
    for row in rows:
        _, _, latitude, longitude, population = row.rstrip("\n").split("\t")
        place = (float(latitude), float(longitude), int(population))
        places.append(place)
        grid[math.floor(place[0]), math.floor(place[1])].append(place)

    def satellite(latitude: float, longitude: float, population: int) -> bool:
        row, column = math.floor(latitude), math.floor(longitude)
        return any(
            other[2] >= SATELLITE_RATIO * population
            and great_circle_km(latitude, longitude, other[0], other[1]) <= SATELLITE_KM
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
            for other in grid.get((row + dy, column + dx), ())
        )

    return [row for row, place in zip(rows, places, strict=True) if not satellite(*place)]


def main() -> None:
    regions = {}
    for line in download("admin1CodesASCII.txt").decode().splitlines():
        code, name, _, geonameid = line.split("\t")
        regions[geonameid] = (code, name)
    languages = {}
    for line in download("countryInfo.txt").decode().splitlines():
        if not line.startswith("#"):
            field = line.split("\t")
            languages[field[0]] = field[15].split(",")[0].split("-")[0]
    names = defaultdict(list)
    with zipfile.ZipFile(io.BytesIO(download("alternateNamesV2.zip"))) as archive:
        with archive.open("alternateNamesV2.txt") as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                field = line.rstrip("\n").split("\t")
                if field[1] in regions:
                    names[field[1]].append(field)
    region_names = {
        code: local_name(code, english, names[geonameid], languages.get(code[:2], ""))
        for geonameid, (code, english) in regions.items()
    }
    with zipfile.ZipFile(io.BytesIO(download("cities15000.zip"))) as archive:
        cities = archive.read("cities15000.txt").decode()

    rows = []
    for line in cities.splitlines():
        field = line.split("\t")
        name, latitude, longitude, feature, country, admin1, population = (
            field[1],
            field[4],
            field[5],
            field[7],
            field[8],
            field[10],
            field[14],
        )
        if feature in ("PPLX", "PPLH"):
            continue
        region = region_names.get(f"{country}.{admin1}", "")
        rows.append(f"{name}\t{region}\t{latitude}\t{longitude}\t{population or 0}\n")
    rows = without_satellites(rows)

    # mtime=0 keeps the file the same when GeoNames has not changed.
    with open(TARGET, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as file:
        file.write("".join(rows).encode())
    print(f"{len(rows)} places written to {TARGET}")


if __name__ == "__main__":
    main()
