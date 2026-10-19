"""Nodes that turned OK to MQTT off (opted-out nodes): deciding which they
are, and what pages use so that no response ever names one.

Every packet a node sends carries its OK to MQTT setting (Data.bitfield bit
0), off unless its owner turns it on. Gateways on firmware 2.7.20 and newer
do not upload other nodes' packets with it off; older ones do. History keeps
everything: a node is opted out while its newest decoded packet, captured
(packet.ok_to_mqtt) or surveyed (coverage_measurement.ok_to_mqtt), had the
bit off, so turning it on shows its whole history again and turning it off
hides it all. A node reporting to MQTT as a gateway uploads its own packets
whatever the bit, so it never counts as opted out.

Pages leave out an opted-out node's own rows (the node, its packets and
links) and show its place in other nodes' data (route hops, neighbour
lists, relays) as an unknown node's. It reads like a node never heard;
nothing says why.
"""

import json
import sqlite3
from collections.abc import Iterable


def opted_out_sql(node: str) -> str:
    """An SQL condition: the node in the column or expression `node` opted
    out; false for NULL. IN, not EXISTS: inside an EXISTS a bare node_id
    would name node_opt_out's own column."""
    return f"coalesce({node} IN (SELECT node_id FROM node_opt_out), 0)"


def shown_sql(node: str) -> str:
    """An SQL condition: the node in `node` did not opt out."""
    return f"NOT {opted_out_sql(node)}"


# The packets pages may show, for a query on packet with no alias: neither
# end opted out.
SHOWN_PACKET = f"{shown_sql('sender_node_id')} AND {shown_sql('recipient_node_id')}"


def unshown_packets(connection: sqlite3.Connection, packet_ids: Iterable[int | None]) -> set[int]:
    """Those of packet_ids that pages do not show (SHOWN_PACKET). A link to one
    would open "Packet not found", so pages keep its time and drop the link."""
    ids = sorted({int(packet_id) for packet_id in packet_ids if packet_id is not None})
    if not ids:
        return set()
    return {
        int(row[0])
        for row in connection.execute(
            f"""
            SELECT id FROM packet
            WHERE id IN (SELECT value FROM json_each(?)) AND NOT ({SHOWN_PACKET})
            """,
            (json.dumps(ids),),
        )
    }


def opted_out(connection: sqlite3.Connection) -> set[int]:
    """Every opted-out node. They are hundreds at most, so a request reads
    them all once rather than asking about each node it shows."""
    return {int(row[0]) for row in connection.execute("SELECT node_id FROM node_opt_out")}


def is_opted_out(connection: sqlite3.Connection, node: int) -> bool:
    return (
        connection.execute("SELECT 1 FROM node_opt_out WHERE node_id = ?", (node,)).fetchone()
        is not None
    )


def refresh_opt_outs(connection: sqlite3.Connection, nodes: Iterable[int] | None) -> None:
    """Decide again whether each of nodes opted out; every node when None.
    The caller holds the transaction."""
    if nodes is None:
        connection.execute("DELETE FROM node_opt_out")
        # Only a node with a packet that had the bit off can have opted out.
        nodes = [
            int(row[0])
            for row in connection.execute(
                """
                SELECT sender_node_id FROM packet WHERE ok_to_mqtt = 0
                UNION SELECT sender_node_id FROM coverage_measurement WHERE ok_to_mqtt = 0
                """
            )
        ]
    for node in nodes:
        if _newest_bit(connection, node) == 0 and not _is_gateway(connection, node):
            connection.execute("INSERT OR IGNORE INTO node_opt_out (node_id) VALUES (?)", (node,))
        else:
            connection.execute("DELETE FROM node_opt_out WHERE node_id = ?", (node,))


def _newest_bit(connection: sqlite3.Connection, node: int) -> int | None:
    """The bit of the node's newest packet that told it, captured or surveyed;
    None when none did. Each side reads back from the node's newest packet."""
    row = connection.execute(
        """
        SELECT ok_to_mqtt FROM (
            SELECT * FROM (
                SELECT ok_to_mqtt, first_received_at AS at FROM packet
                WHERE sender_node_id = :node AND ok_to_mqtt IS NOT NULL
                ORDER BY id DESC LIMIT 1
            )
            UNION ALL
            SELECT * FROM (
                SELECT ok_to_mqtt, heard_at FROM coverage_measurement
                WHERE sender_node_id = :node AND ok_to_mqtt IS NOT NULL
                ORDER BY heard_at DESC LIMIT 1
            )
        )
        ORDER BY at DESC LIMIT 1
        """,
        {"node": node},
    ).fetchone()
    return None if row is None else int(row[0])


def _is_gateway(connection: sqlite3.Connection, node: int) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM node_current WHERE node_id = ? AND last_gateway_at IS NOT NULL",
            (node,),
        ).fetchone()
        is not None
    )
