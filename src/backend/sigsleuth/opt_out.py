"""Nodes that turned OK to MQTT off (opted-out nodes): deciding which they
are, and what pages use so that no response ever names one.

Every packet a node sends carries its OK to MQTT setting (Data.bitfield bit
0), off unless its owner turns it on. Gateways on firmware 2.7.20 and newer
do not upload other nodes' packets with it off; older ones do. History keeps
everything: a node is opted out while the newest bit it told, in a captured
packet or a survey measurement, is off (node_reported.ok_to_mqtt, kept after
the packet expires), so turning it on shows its whole history again and
turning it off hides it all. A node reporting to MQTT as a gateway uploads its own packets
whatever the bit, so it never counts as opted out.

Pages leave out an opted-out node's own rows (the node, its packets and
links) and show its place in other nodes' data (route hops, neighbour
lists, relays) as an unknown node's. It reads like a node never heard;
nothing says why.

With SIGSLEUTH_HIDE_OPTED_OUT off, node_opt_out stays empty, so every page
shows every node. Startup refills the table, as the setting may have changed.
"""

import json
import sqlite3
from collections.abc import Iterable

# SIGSLEUTH_HIDE_OPTED_OUT, set at startup.
hiding = True


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


def unlinkable_packets(
    connection: sqlite3.Connection, packet_ids: Iterable[int | None]
) -> set[int]:
    """Those of packet_ids that pages do not show (SHOWN_PACKET) or that
    retention deleted. A link to one would open "Packet not found", so pages
    keep its time and drop the link."""
    ids = {int(packet_id) for packet_id in packet_ids if packet_id is not None}
    if not ids:
        return set()
    return ids - {
        int(row[0])
        for row in connection.execute(
            f"""
            SELECT id FROM packet
            WHERE id IN (SELECT value FROM json_each(?)) AND {SHOWN_PACKET}
            """,
            (json.dumps(sorted(ids)),),
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


# An opted-out node_reported row r: its bit is off and it is no gateway.
_OPTED_OUT = """
    r.ok_to_mqtt = 0 AND NOT EXISTS (
        SELECT 1 FROM node_current AS n
        WHERE n.node_id = r.node_id AND n.last_gateway_at IS NOT NULL
    )
"""


def refresh_opt_outs(connection: sqlite3.Connection, nodes: Iterable[int] | None) -> None:
    """Decide again whether each of nodes opted out (_OPTED_OUT); every node
    when None. Without hiding, none ever is. The caller holds the transaction."""
    if nodes is None:
        connection.execute("DELETE FROM node_opt_out")
        if hiding:
            connection.execute(
                f"""
                INSERT INTO node_opt_out
                SELECT r.node_id FROM node_reported AS r WHERE {_OPTED_OUT}
                """
            )
        return
    if not hiding:
        return
    listed = json.dumps(sorted(set(nodes)))
    connection.execute(
        "DELETE FROM node_opt_out WHERE node_id IN (SELECT value FROM json_each(?))", (listed,)
    )
    connection.execute(
        f"""
        INSERT INTO node_opt_out
        SELECT r.node_id FROM json_each(?) AS j JOIN node_reported AS r ON r.node_id = j.value
        WHERE {_OPTED_OUT}
        """,
        (listed,),
    )
