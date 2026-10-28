"""Rows written straight into tables, named by column, so adding a column
to a table breaks no test that leaves it out."""

import sqlite3
from collections.abc import Sequence

HOUR_MS = 3_600_000


def insert_row(connection: sqlite3.Connection, table: str, **values: object) -> None:
    columns = ", ".join(values)
    names = ", ".join(f":{column}" for column in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({names})", values)


def link_hour(
    connection: sqlite3.Connection,
    from_node_id: int,
    to_node_id: int,
    last_seen_at: int,
    *,
    link_kind: str = "direct",
    observations: int = 1,
    snr: Sequence[float] = (),
    channel: str = "LongFast",
    km: float | None = None,
) -> None:
    """from_node_id heard by to_node_id, in last_seen_at's hour; snr holds
    the measured observations' SNRs."""
    insert_row(
        connection,
        "link_hour",
        link_kind=link_kind,
        hour_start=last_seen_at - last_seen_at % HOUR_MS,
        from_node_id=from_node_id,
        to_node_id=to_node_id,
        observations=observations,
        snr_count=len(snr),
        snr_sum=sum(snr),
        rssi_count=0,
        rssi_sum=0.0,
        last_seen_at=last_seen_at,
        channel=channel,
        km=km,
    )


def node_position(
    connection: sqlite3.Connection, node_id: int, at: int, latitude: float, longitude: float
) -> None:
    """A position node_id reported at `at`."""
    insert_row(
        connection,
        "node_position",
        node_id=node_id,
        at=at,
        latitude=latitude,
        longitude=longitude,
        packet_row_id=1,
    )
