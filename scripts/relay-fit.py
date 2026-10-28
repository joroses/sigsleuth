"""Fit relay naming's weights (relays.WEIGHTS and UNKNOWN_SCORE) on route truth.

Usage: uv run --locked --with numpy python scripts/relay-fit.py DATABASE [--every 8]

Truth is every relayed traceroute reception whose own route names its relay
(relay_checks.route_relay), on every `every`-th traceroute. Each copy is read
twice:

- survey: a receiver at the gateway with no links of its own. The gateway's
  own report, its links in candidates' reach and the sender's traceroute
  hops from the packet's hour on are left out, so the copy's own route
  cannot name its relay;
- page: as packet pages read it, with the gateway's direct receptions
  (heard directly first when it heard exactly one candidate, unless
  packets.skips_shortcut).

The fit is a conditional logit over each copy's possible candidates and a
node SigSleuth does not know. Route truth never has an unknown relay, so
each copy also enters with its true relay removed, labelled unknown,
weighted by PI, the assumed share of relays nobody knows. One table serves
both modes. Figures are out of fold: fitted on one half of the packets by
time, tested on the other, then the other way round. "Hidden" counts the
names given with the true relay removed: any is wrong. The table printed
last is fitted on all copies. Read-only; about 30 minutes on 14 days of a
busy mesh with --every 8.
"""

import argparse
import sqlite3
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from sigsleuth import packets, positions, relays
from sigsleuth.db import to_epoch_ms
from sigsleuth.derive import HOUR_MS
from sigsleuth.opt_out import SHOWN_PACKET
from sigsleuth.relay_checks import route_relay

PI = 0.01
L2 = 1.0
TERMS: tuple[relays.RelayTerm, ...] = tuple(relays.WEIGHTS)
INDEX = {term: index for index, term in enumerate(TERMS)}
MODES = ("survey", "page")


@dataclass
class Copy:
    """One copy in one mode: its candidates' terms and the route's relay.
    heard_one is the candidate named for being the only one the gateway heard
    directly (page mode), heard_hidden the one when the true relay is removed."""

    packet_at: int
    mode: str
    truth: int
    terms: dict[int, dict[relays.RelayTerm, float]]
    heard_one: int | None = None
    heard_hidden: int | None = None


def shortcut(
    naming: packets.ReceptionNaming,
    heard: list[int],
    signals: Mapping[tuple[int, int], relays.DirectSignal],
) -> int | None:
    """The node packet pages name without scoring when the gateway heard
    only `heard` directly."""
    if len(heard) == 1 and not packets.skips_shortcut(naming.copy, heard[0], signals):
        return heard[0]
    return None


def collect(connection: sqlite3.Connection, every: int) -> list[Copy]:
    ids = [
        int(row[0])
        for row in connection.execute(
            f"SELECT id FROM packet WHERE portnum = 70 AND {SHOWN_PACKET} ORDER BY id DESC"
        )
    ][::every]
    copies: list[Copy] = []
    start = time.monotonic()
    for index, packet_row_id in enumerate(ids):
        if index % 500 == 0:
            print(
                f"{index:,} / {len(ids):,} traceroutes, {len(copies):,} copies, "
                f"{time.monotonic() - start:.0f} s",
                flush=True,
            )
        found = packets.relayed_packet(connection, packet_row_id)
        if found is None or found.relays is None:
            continue
        evidence, signals = found.relays.evidence, found.relays.signals
        truths = {
            reception.reception_id: truth[0]
            for reception in found.receptions
            if reception.reception_id in found.relays.namings
            and (truth := route_relay(reception)) is not None
        }
        if not truths:
            continue
        at = to_epoch_ms(found.packet.first_received_at)
        hour = at - at % HOUR_MS
        sender = found.packet.sender_node_id
        candidates = {node for nodes in evidence.by_byte.values() for node in nodes}
        partners = positions.reach_by_partner(connection, candidates, from_km=relays.NAME_KM)
        hops_before = positions.traceroute_hops(
            connection,
            {sender},
            candidates,
            around=hour - 1 - positions.RECENT_HOURS * HOUR_MS,
        )
        for reception_id, truth in truths.items():
            naming = found.relays.namings[reception_id]
            gateway = naming.copy.receiver
            heard = sorted(naming.heard)
            copies.append(
                Copy(
                    at,
                    "page",
                    truth,
                    {node: dict(terms) for node, terms in naming.scores.terms.items()},
                    shortcut(naming, heard, signals),
                    shortcut(naming, [node for node in heard if node != truth], signals),
                )
            )
            survey_evidence = replace(
                evidence,
                reach={
                    node: max(km for partner, km in reach.items() if partner != gateway)
                    for node, reach in partners.items()
                    if any(partner != gateway for partner in reach)
                },
                sender_hops=hops_before,
            )
            copy = replace(
                naming.copy,
                receiver=0,
                reports=[report for report in naming.copy.reports if report[0] != gateway],
            )
            copies.append(
                Copy(at, "survey", truth, relays.relay_terms(survey_evidence, copy, signals))
            )
    print(f"{len(copies):,} copies in {time.monotonic() - start:.0f} s", flush=True)
    return copies


def matrix(copy: Copy, without: int | None = None) -> tuple[list[int], np.ndarray]:
    """The copy's candidates (but `without`) and their term vectors, with
    the unknown node's last."""
    nodes = [node for node in copy.terms if node != without]
    rows = np.zeros((len(nodes) + 1, len(TERMS) + 1))
    for row, node in enumerate(nodes):
        for term, value in copy.terms[node].items():
            rows[row, INDEX[term]] = value
    rows[len(nodes), len(TERMS)] = 1
    return nodes, rows


def fit(copies: list[Copy]) -> np.ndarray:
    """Newton's method on the weighted conditional log likelihood; the last
    weight is the unknown node's score."""
    mats, labels, weights = [], [], []
    for copy in copies:
        nodes, rows = matrix(copy)
        if copy.truth in nodes:
            mats.append(rows)
            labels.append(nodes.index(copy.truth))
            weights.append(1.0)
            mats.append(np.delete(rows, nodes.index(copy.truth), axis=0))
            labels.append(len(nodes) - 1)
            weights.append(PI / (1 - PI))
    sizes = np.array([len(m) for m in mats])
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    x = np.vstack(mats)
    label_rows = starts + np.array(labels)
    weight = np.array(weights)
    group = np.repeat(np.arange(len(starts)), sizes)
    w = np.zeros(x.shape[1])

    def likelihood(w: np.ndarray) -> tuple[float, np.ndarray]:
        u = x @ w
        top = np.maximum.reduceat(u, starts)
        e = np.exp(u - top[group])
        total = np.add.reduceat(e, starts)
        p = e / total[group]
        ll = np.sum(weight * (u[label_rows] - top - np.log(total))) - L2 / 2 * w @ w
        return ll, p

    ll, p = likelihood(w)
    for _ in range(100):
        expect = np.add.reduceat(x * p[:, None], starts)
        grad = (weight[:, None] * (x[label_rows] - expect)).sum(0) - L2 * w
        hess = (
            x.T @ (x * (p * weight[group])[:, None])
            - expect.T @ (expect * weight[:, None])
            + L2 * np.eye(len(w))
        )
        step = np.linalg.solve(hess, grad)
        scale = 1.0
        while True:
            new_ll, new_p = likelihood(w + scale * step)
            if new_ll >= ll - 1e-9 or scale < 1e-4:
                break
            scale /= 2
        w, ll, p = w + scale * step, new_ll, new_p
        if np.max(np.abs(scale * step)) < 1e-7:
            break
    return w


def named(copy: Copy, w: np.ndarray, without: int | None = None) -> int | None:
    """What relays.RelayScores.named names with weights w."""
    nodes, rows = matrix(copy, without)
    u = rows @ w
    p = np.exp(u - u.max())
    p /= p.sum()
    probabilities: dict[int | None, float] = {node: float(p[i]) for i, node in enumerate(nodes)}
    probabilities[None] = float(p[-1])
    terms = {node: copy.terms[node] for node in nodes}
    found = relays.RelayScores(terms, probabilities).named()
    return None if found is None else found[0]


def tally(copies: list[Copy], w: np.ndarray) -> Counter[str]:
    counts: Counter[str] = Counter()
    for copy in copies:
        name = copy.heard_one if copy.heard_one is not None else named(copy, w)
        counts["undecided" if name is None else "right" if name == copy.truth else "wrong"] += 1
        hidden = copy.heard_hidden
        if hidden is None:
            hidden = named(copy, w, without=copy.truth)
        counts["hidden"] += hidden is not None
    return counts


def line(label: str, counts: Counter[str]) -> str:
    named_count = counts["right"] + counts["wrong"]
    return (
        f"{label}: right {counts['right']:,}, wrong {counts['wrong']:,}, "
        f"undecided {counts['undecided']:,}, right among named "
        f"{100 * counts['right'] / max(1, named_count):.2f}%, "
        f"wrong with the true relay hidden {counts['hidden']:,}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--every", type=int, default=8)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    np.seterr(over="ignore")
    copies = collect(connection, args.every)
    if not copies:
        print("No relayed traceroute receptions with route truth.")
        return
    times = sorted({copy.packet_at for copy in copies})
    middle = times[len(times) // 2]
    halves = (
        [copy for copy in copies if copy.packet_at < middle],
        [copy for copy in copies if copy.packet_at >= middle],
    )
    shipped = np.array([*(relays.WEIGHTS[term] for term in TERMS), relays.UNKNOWN_SCORE])
    print(f"\nAt p ≥ {relays.NAME_PROBABILITY}, names need evidence:")
    for mode in MODES:
        out_of_fold: Counter[str] = Counter()
        for train, test in (halves, halves[::-1]):
            w = fit(train)
            out_of_fold += tally([copy for copy in test if copy.mode == mode], w)
        print(line(f"{mode}, refitted out of fold", out_of_fold))
        print(
            line(f"{mode}, relays.WEIGHTS", tally([c for c in copies if c.mode == mode], shipped))
        )
    w = fit(copies)
    print(f"\nFitted on {len(copies) // 2:,} copies in each mode, PI = {PI}:\n")
    for term, weight in zip(TERMS, w[:-1], strict=True):
        print(f'    "{term}": {weight:.2f},')
    print(f"UNKNOWN_SCORE = {w[-1]:.2f}")


if __name__ == "__main__":
    main()
