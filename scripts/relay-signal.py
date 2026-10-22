"""Measure the "too strong" relay rule-out (coverage.too_strong), read-only,
on a copy of a live database. docs/relay-evidence.md records the results.

Usage: uv run --locked python scripts/relay-signal.py DATABASE

The reporting gateway stands in for the phone. The truth is the route
truth of scripts/relay-evidence.py: a relayed traceroute reception whose
route names its last relay, when that relay sends the reception's byte.
Candidates are node_current's nodes sending the byte, other than the sender
and the gateway, that the gateway heard directly (no hops used) in the
HISTORY_MS before the reception. A candidate is ruled out at a margin when
the reception's signal is more than that above the strongest of those
readings; "too weak" checks the weakest from below instead. The direct
receptions are loaded once (millions of rows, kept in arrays).
"""

import argparse
import math
import sqlite3
from array import array
from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import closing

from sigsleuth.coverage import signal_db
from sigsleuth.decode import read_route_discovery
from sigsleuth.derive import direct_reception_sql
from sigsleuth.radio import relay_byte_of
from sigsleuth.traceroute import UNKNOWN_NODE

DAY_MS = 86_400_000
HISTORY_MS = 30 * DAY_MS
MARGINS_DB = (10, 15, 20, 25, 30, 40)
# The reading variants count candidates with this many readings or more.
VARIANT_READINGS = 3
TRACEROUTE_PORT = 70


class Readings:
    """One (node, gateway) pair's direct receptions, in time order: when,
    signal (coverage.signal_db), RSSI and SNR (NaN when unknown)."""

    def __init__(self) -> None:
        self.times = array("q")
        self.signal = array("d")
        self.rssi = array("d")
        self.snr = array("d")


def load_direct(connection: sqlite3.Connection) -> dict[tuple[int, int], Readings]:
    rows = connection.execute(
        f"""
        SELECT p.sender_node_id, r.gateway_node_id, r.received_at, r.rx_rssi, r.rx_snr
        FROM reception AS r JOIN packet AS p ON p.id = r.packet_row_id
        WHERE {direct_reception_sql("r", "p")} AND r.rx_rssi != 0
        ORDER BY r.received_at
        """
    )
    direct: dict[tuple[int, int], Readings] = defaultdict(Readings)
    for sender, gateway, at, rssi, snr in rows:
        readings = direct[int(sender), int(gateway)]
        readings.times.append(int(at))
        readings.signal.append(signal(rssi, snr))
        readings.rssi.append(float(rssi))
        readings.snr.append(math.nan if snr is None else float(snr))
    return direct


def signal(rssi: int, snr: float | None) -> float:
    """coverage.signal_db of a reading with an RSSI (both queries ask for one)."""
    value = signal_db(rssi, snr)
    assert value is not None
    return float(value)


def route_truth(
    payload: bytes | None, gateway: int, request_id: int | None, byte: int
) -> int | None:
    """The last relay a gateway's traceroute route names, when it sends the byte."""
    discovery = None if payload is None else read_route_discovery(bytes(payload))
    if discovery is None:
        return None
    route = list(discovery.route_back if request_id is not None else discovery.route)
    if len(route) >= 8:
        return None
    relays = route[:-1] if route and route[-1] == gateway else route
    if not relays or relays[-1] == UNKNOWN_NODE or relay_byte_of(relays[-1]) != byte:
        return None
    return int(relays[-1])


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))] if ordered else math.nan


def ruled_out(values: list[float], margin: float) -> str:
    return "-" if not values else f"{sum(v > margin for v in values) / len(values):.3%}"


def measure(connection: sqlite3.Connection) -> None:
    direct = load_direct(connection)
    by_byte: dict[int, list[int]] = defaultdict(list)
    for (node,) in connection.execute("SELECT node_id FROM node_current"):
        by_byte[relay_byte_of(int(node))].append(int(node))

    cases = 0
    true_heard = 0
    # Per readings floor (1, 3): how far each candidate's reading sat above
    # its strongest direct one, for the true relay and the others.
    excess: dict[tuple[int, bool], list[float]] = defaultdict(list)
    # Per (reading, side): how far past the strongest (too strong) or the
    # weakest (too weak) direct reading, SNR known on both sides.
    variants: dict[tuple[str, str, bool], list[float]] = defaultdict(list)
    # Per margin (None: no rule), right and wrong names.
    named: dict[int | None, Counter[bool]] = defaultdict(Counter)
    rows = connection.execute(
        """
        SELECT r.received_at, t.sender_node_id, t.request_id, r.gateway_node_id,
               r.rx_snr, r.rx_rssi, r.relay_byte, r.route_payload
        FROM reception AS r JOIN packet AS t ON t.id = r.packet_row_id
        WHERE coalesce(t.portnum, -1) = ? AND r.route_payload IS NOT NULL
          AND r.hop_start > r.hop_limit AND r.relay_byte IS NOT NULL AND r.rx_rssi != 0
        """,
        (TRACEROUTE_PORT,),
    )
    for at, sender, request_id, gateway, snr, rssi, byte, payload in rows:
        if gateway == sender or byte == relay_byte_of(gateway):
            continue
        truth = route_truth(payload, gateway, request_id, byte)
        if truth is None:
            continue
        cases += 1
        now_signal = signal(rssi, snr)
        heard = []
        for node in by_byte.get(byte, []):
            readings = direct.get((node, gateway))
            if node in (sender, gateway) or readings is None:
                continue
            first = bisect_left(readings.times, at - HISTORY_MS)
            last = bisect_left(readings.times, at)
            if last <= first:
                continue
            wrong = node != truth
            over = now_signal - max(readings.signal[first:last])
            heard.append((node, over))
            for floor in (1, VARIANT_READINGS):
                if last - first >= floor:
                    excess[floor, wrong].append(over)
            if snr is None:
                continue
            known = [i for i in range(first, last) if not math.isnan(readings.snr[i])]
            if len(known) < VARIANT_READINGS:
                continue
            now = {"signal": now_signal, "rssi": rssi, "snr": snr}
            for reading, values in (
                ("signal", readings.signal),
                ("rssi", readings.rssi),
                ("snr", readings.snr),
            ):
                past = [values[i] for i in known]
                variants[reading, "strong", wrong].append(now[reading] - max(past))
                variants[reading, "weak", wrong].append(min(past) - now[reading])
        true_heard += any(node == truth for node, _ in heard)
        # The survey-link stand-in: a name when exactly one candidate is
        # heard directly, or is left by the rule.
        for margin in (None, *MARGINS_DB):
            left = [node for node, over in heard if margin is None or over <= margin]
            if len(left) == 1:
                named[margin][left[0] == truth] += 1

    print(f"Route-truth relayed receptions with an RSSI: {cases:,}")
    print(f"  true relay heard directly in the {HISTORY_MS // DAY_MS} days before: {true_heard:,}")
    for floor in (1, VARIANT_READINGS):
        relays, others = excess[floor, False], excess[floor, True]
        print(
            f"\nCandidates with {floor} or more direct readings: "
            f"true {len(relays):,}, wrong {len(others):,}"
        )
        print(
            f"  true relay over its strongest: p50 {percentile(relays, 0.5):.1f}, "
            f"p99 {percentile(relays, 0.99):.1f}, p99.9 {percentile(relays, 0.999):.1f} dB"
        )
        for margin in MARGINS_DB:
            print(
                f"  {margin:>2} dB: true relays ruled out {ruled_out(relays, margin)}, "
                f"wrong candidates ruled out {ruled_out(others, margin)}"
            )
    print(
        f"\nBy reading, {VARIANT_READINGS}+ readings with SNR (true relays lost / wrong removed):"
    )
    for reading in ("signal", "rssi", "snr"):
        for side in ("strong", "weak"):
            relays, others = variants[reading, side, False], variants[reading, side, True]
            cells = ", ".join(
                f"{m}: {ruled_out(relays, m)} / {ruled_out(others, m)}" for m in MARGINS_DB[:5]
            )
            print(f"  {reading:6} too {side:6} {cells}")
    print("\nSurvey-link stand-in, a name when exactly one candidate is left:")
    for margin, counts in named.items():
        rule = "without the rule" if margin is None else f"{margin:>2} dB"
        print(f"  {rule}: right {counts[True]:,}, wrong {counts[False]:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database")
    arguments = parser.parse_args()
    with closing(sqlite3.connect(f"file:{arguments.database}?mode=ro", uri=True)) as connection:
        measure(connection)


if __name__ == "__main__":
    main()
