# Coverage

How the map's coverage layer turns signal surveys into coloured squares (`coverage.py`). Setup is in the [README](../README.md#coverage).

## What a square shows

A survey records each packet its node hears, with the phone's position: a measurement. Uploads drop measurements whose GPS fix is worse than 100 m or older than 10 s.

A square's colour is the average margin of its measurements: how far the SNR sits above the weakest signal the survey node can decode at its spreading factor (−17.5 dB on LongFast). Margins compare across presets where raw SNR does not. The scale runs from red at 0 dB (barely decoded) through amber to dark green at 25 dB or more. Grey means no SNR, or a survey that did not send its radio settings. When every square in view comes from surveys on one spreading factor, the legend and a square's hover read in SNR instead (the same colours, as margin is then SNR plus a constant).

- The SNR is that of the transmission that reached the phone, so it measures the node heard (below), not the sender of a relayed packet.
- The average covers every node heard there, so a busy node nearby weighs most. With a node selected, the map shows only where it was heard and how strongly ("Heard from it", the default; "All nodes" shows everything). Choosing a node on the map, in the search or in "Heard in view" moves the map out to it and where it was heard, if they are not all in view; "Fit map to coverage" zooms to them. The node page's minimap shows the same. The sidebar's "Heard in view" lists the nodes heard in the squares in view, in most squares first, whatever is selected. While coverage is on, the map names only those nodes and the selected one; the others keep their dots.
- The map's time window does not apply: measurements are kept `SIGSLEUTH_COVERAGE_RETENTION_DAYS` (90). Its channel filter does: it drops measurements whose sender's latest channel is another one, and surveys on another spreading factor than the channel's.

## Which node was heard

1. **Directly**: the survey node heard the sender itself, by the rule links use: no hops used, the relay byte is the sender's own, and a signal was measured.
2. **Survey link**: a relayed packet names only the last byte of its relay's number. When exactly one node with that byte was heard directly by a survey node on the same radio settings within 30 km and an hour of the measurement (or, if none, 60 days), it is the relay. A node is left out when the packet arrived more than 30 dB stronger (RSSI, plus the SNR when negative) than the same survey node heard it directly within 1 km (60 days either side): a relay cannot be much louder than its own packets from the same place, though it can be quieter. Later uploads can add or change survey links.
3. **Guessed from MQTT**: otherwise, the [relay evidence](relay-evidence.md) rules name one at upload, once, from what MQTT knew then: they rule out candidates that could not have relayed it there (the 30 dB rule too), then use the gateways' reports of the same packet and their radio links, or the only candidate left.
4. **Unknown**: none names one. The square's details then suggest up to three nodes with that byte nearest it that could have relayed it.

Clicking a square, also through a node's name drawn over it, draws lines from its centre to where each heard node is now: solid for 1 and 2, dashed for 3. Its details say how each node was named.

## Which measurements count

A measurement counts unless it is proven stale, and only while its node is known to be alive or the measurement is recent:

- **Node seen by MQTT** in the last `SIGSLEUTH_RETENTION_DAYS` (14): counts if heard after the node's last move over 25 km.
- **Otherwise** (MQTT has not seen it in 14 days, or the heard node is unknown): counts if under 14 days old, or if a survey heard the node send a packet, directly or relayed, in the last 14 days.

Not being seen by MQTT does not mean a node is gone: surveys matter most where no gateway listens. A doubtful position does not hide a node's measurements, since a wrong position does not make a reception wrong. Measurements that do not count are hidden, not deleted.

## Nodes that do not allow MQTT

A node's OK to MQTT setting travels in every packet it sends (`Data.bitfield` bit 0). No page names a node that turned it off (an [opted-out node](glossary.md#nodes-and-positions)), wherever its packets came from. A survey node hears its packets over the air, so each measurement keeps the bit, unknown when the survey node could not decode the packet. A missing bit counts as off, as in the firmware. Capture keeps the bit of each packet too, and the newer packet decides, captured or surveyed.

- A node whose newest decoded packet has it off is never named, but its measurements still colour the squares, past ones included. A square's details list them as an unknown relay with its relay byte, like any other, and say nothing of the setting. Selecting the node ("Heard from it", its minimap) shows none. Turning the bit back on names them again.
- The bit is the sender's, and a square measures the node heard. So a packet from a node that opted out, relayed by one that did not, is named by the relay, without a link to the packet, whose page names the sender.
- A node no packet has told the bit of is named, and so is a node reporting to MQTT as a gateway, which uploads its own packets whatever the bit.
- A node that opted out is not suggested as a possible relay for its byte. Survey links still name it as a relay, so the packets it relayed are not credited to another node.
- Packet pages, and the gateway reports in chat, never name it as a gateway's relay either: a copy it relayed reads as undecided, with the other possible relays.

## Limits

- A node that stops working keeps its squares for up to 14 days.
- Moves are noticed only while MQTT sees the node. One that moved while unseen, or in steps under 25 km, keeps its old squares.
- The survey link bounds (30 km, one hour, 60 days) are not checked against known relays, and the 30 dB rule only on gateways, which sit higher and move less than a phone. `scripts/coverage-backtest.py DATABASE` shows how much the rules hide and how other bounds compare.
- A node that turns OK to MQTT off stays named until a survey or a gateway reports one of its packets with the bit, and one that turns it back on stays unnamed until then. Gateways on firmware 2.7.20 and newer upload no packet with the bit off, so a node behind them is named until a survey hears it.
- A node that opted out shows its relay byte, the last byte of its number, which about 1 in 256 nodes share.
- Anyone who can reach the page sees the squares and when they were measured. A survey is a track of where the surveyor was.
