# Coverage

How the map's coverage layer turns signal surveys into coloured squares (`coverage.py`). Setup is in the [README](../README.md#coverage).

## What a square shows

A survey records each packet its node hears, with the phone's position: a measurement. Uploads drop measurements whose GPS fix is worse than 100 m or older than 10 s.

A square's colour is the average margin of its measurements: how far the SNR sits above the weakest signal the survey node can decode at its spreading factor (−17.5 dB on LongFast). Margins compare across presets where raw SNR does not. The scale runs from red at 0 dB (barely decoded) through amber to dark green at 25 dB or more. Grey means no SNR, or a survey that did not send its radio settings.

- The SNR is that of the transmission that reached the phone, so it measures the node heard (below), not the sender of a relayed packet.
- The average covers every node heard there, so a busy node nearby weighs most. With a node selected, the map shows only where it was heard and how strongly ("Heard from it", the default; "All nodes" shows everything). The node page's minimap shows the same.
- The map's time window does not apply: measurements are kept `SIGSLEUTH_COVERAGE_RETENTION_DAYS` (90). Its channel filter does: it drops measurements whose sender's latest channel is another one, and surveys on another spreading factor than the channel's.

## Which node was heard

1. **Directly**: the survey node heard the sender itself, by the rule links use: no hops used, the relay byte is the sender's own, and a signal was measured.
2. **Survey link**: a relayed packet names only the last byte of its relay's number. When exactly one node with that byte was heard directly by a survey node on the same radio settings within 30 km and an hour of the measurement (or, if none, 60 days), it is the relay. Later uploads can add or change survey links.
3. **Guessed from MQTT**: otherwise, the [relay evidence](relay-evidence.md) rules name one at upload, once, from what MQTT knew then: they rule out candidates that could not have relayed it there, then use the gateways' reports of the same packet and their radio links, or the only candidate left.
4. **Unknown**: none names one. The square's details then suggest up to three nodes with that byte nearest it that could have relayed it.

Clicking a square draws lines from its centre to where each heard node is now: solid for 1 and 2, dashed for 3. Its details say how each node was named.

## Which measurements count

A measurement counts unless it is proven stale, and only while its node is known to be alive or the measurement is recent:

- **Node seen by MQTT** in the last `SIGSLEUTH_RETENTION_DAYS` (14): counts if heard after the node's last move over 25 km and its last flight.
- **Otherwise** (MQTT has not seen it in 14 days, or the heard node is unknown): counts if under 14 days old, or if a survey heard the node send a packet, directly or relayed, in the last 14 days.

Not being seen by MQTT does not mean a node is gone: surveys matter most where no gateway listens. A doubtful position does not hide a node's measurements, since a wrong position does not make a reception wrong. Measurements that do not count are hidden, not deleted.

## Limits

- A node that stops working keeps its squares for up to 14 days.
- Moves are noticed only while MQTT sees the node. One that moved while unseen, or in steps under 25 km, keeps its old squares.
- The survey link bounds (30 km, one hour, 60 days) are not checked against known relays. `scripts/coverage-backtest.py DATABASE` shows how much the rules hide and how other bounds compare.
- Surveys record packets from nodes that do not allow MQTT too. Each measurement keeps that setting, but the map shows them all.
- Anyone who can reach the page sees the squares and when they were measured. A survey is a track of where the surveyor was.
