# Relay evidence: which sources name a relay

Measured 2026-10-17 on a copy of live loranet.pl `msh/#`: 14.6 days to 2026-10-12, 800 k packets, 9.8 M receptions, 4,153 nodes. `scripts/relay-evidence.py DATABASE` prints every figure here (about 13 minutes). Coverage uploads and packet pages named relays by the 2026-10-18 decision below, and by one score since 2026-10-27 ([Decision: one score](#decision-one-score-2026-10-27)). Revisit once surveys hear relays that MQTT names (see the last section), or when firmware changes what gateways upload.

## The problem

A relayed packet names its last relay by one byte (`relay_byte`; a node ending in `00` sends `FF`). On live, 16 nodes share a byte on average (median 11, up to 51: bytes that are multiples of 4 are the commonest), and 31% of placed nodes have another placed node with their byte within 20 km. Coverage needs the node a survey measurement heard; packet pages show the node a gateway heard ("≈" when guessed).

Before this decision:

- Upload (`coverage.heard_node`): `reported`, then `gateway_link`, then `sender_link` on one hop; otherwise unknown, until a survey link names it.
- Packet page (`packets._with_relay_guesses`): radio links to the receiving gateway, then to the sender, then the nearest placed candidate at any distance.
- The own-reach rule (proposed before this study, in no code): name a candidate when it is the only one whose own reach covers the receiver. A node's reach is its longest trusted link, never less than a floor.

## How it was measured

Relayed MQTT receptions stand in for survey measurements, and the reporting gateway stands in for the phone. Gateways sit higher than phones, so they hear farther relays. Candidates are the `node_current` nodes sending the byte, other than the sender and the receiver.

- **Route truth** (the reference): 286,858 relayed traceroute receptions whose route names the last relay, when that relay sends the reception's byte. 99.6% of readable routes agree with the byte. 20,925 receptions were left out because their route was full (8 entries). Bias: these relays sit on traceroute paths. 55% are ROUTERs (4% of other candidates), 90% report a position and 19% are gateways. 2,556 (receiver, relay) pairs; the 10 commonest hold 10%.
- **Reported truth**: 28,267 relayed receptions on every 50th packet where exactly one gateway with the byte reported the packet one hop earlier (`coverage.py`'s `reported`). Bias: every relay is a gateway, and the label itself is wrong a third of the time (below). Figures on it measure agreement with that label, so they appear only where they say something.
- **Hidden**: each source is run again with the true relay removed from the candidates and from every report. This stands for a relay MQTT never sees, so any name is wrong. How many survey relays MQTT never sees is not known yet.
- Links count over the 7 days before the packet, or 24 hours for the `radio_links` sources (as now). Positions are `node_current`'s at the end of the copy (see Staleness).
- A candidate's reach leaves out its own link to the receiver: a phone adds no links.

## What gateways upload

- **Own-byte uploads**: 5.9% of receptions carry the gateway's own relay byte; 71 of 546 gateways do it for over half their receptions. On traceroutes such a gateway has already added itself and lowered the hop limit. It uploads its own rebroadcast's header, so the byte names the gateway, not the node it heard. These receptions say nothing about their relay and are left out. 27,550 of them show one hop used. Most were heard directly: 86% of the 772 such traceroutes list no earlier relay. `is_direct_reception` drops them; counting them would add 226 direct pairs (+5%), one in seven wrong. As evidence for another receiver's relay, though, they read like any other report: a gateway sending the receiver's byte whose report carries it was the relay in 262 of 310 such reports with one more hop left (84.5%) and in 118 of 3,170 with as many (3.7%). Read as the gateway's own rebroadcast, they would name the wrong node (tried 2026-10-18).
- **0-cost hops**: 29.5% of routes list more relays than hops used: routers that favourite each other keep the hop limit. So one hop used does not prove the relay heard the sender (2.8% of route receptions). A relay can also report the packet with as many hops left as the receiver.

## The sources

Can speak: the share of all relayed receptions (every 50th packet, 172 k) where the source names someone. Right: when it names someone, on the route truth. Hidden: the share of route receptions where it names someone after the true relay is removed, which is always a wrong name.

| # | Source (tables) | Can speak | Right | Hidden | Notes and bias |
|---|---|---|---|---|---|
| 1 | One hop used, so the relay heard the sender: the candidate with most radio links to the sender, 24 h (`reception` hop counts, `link_hour`) | 6% | 92% | 0.7% | Only one-hop packets; 0-cost hops break the premise |
| 2a | `reported`: a gateway with the byte reported the packet one hop earlier (`reception`) | 18% | 66% | 7.0% | Proves that gateway relayed, not that the receiver heard its copy. Two same-byte nodes on one flood path often both relay; the errors gather in a few pairs (each pair counted once: 79%). Only one such gateway: 70%. Only one within own reach: 89% |
| 2b | `gateway_link`: radio links to gateways that heard the same rebroadcast (same byte and hops left), 24 h | 55% | 95.5% | 9.5% | Needs co-hearing gateways (56%) |
| 2c | Co-hearers: the only candidate whose own reach covers the receiver and every co-hearer | 32% | 99.8% | 5.9% | |
| 2d | Co-hearers heard it directly: the only candidate a co-hearer heard directly 3 times or more, 7 days (`link_hour` direct) | 48% | 98.7% | 5.6% | 9b without the SNR |
| 9b | Co-hearers' signal: the only candidate that co-hearers hear directly within 6 dB of this rebroadcast's SNR, with every other one checked 10 dB or more off (`reception.rx_snr`, `link_hour` SNR, 3 readings or more) | 49% | 98.8% | 2.7% | The last hop's SNR comes from the relay and the path, not from whose packet it carries. Needs no survey data |
| 3 | `node_current`: role, radio channel, last seen, moves | rule-outs | | | See below |
| 4 | `node_estimate`, `node_doubtful` | places 16% of nodes | | | 9.7% of true relays are placed only by an estimate (ratio 0.62 against other candidates); 7 doubtful nodes |
| 5 | The receiver heard the candidate directly, 7 days (`link_hour` direct) | 87% (sole) | 99.8% | 6.4% | Gateways only: for a survey, this is survey links (any survey node, 30 km, 60 days; untested). The most candidates heard: 93% speak, 97.3% right |
| 6 | Traceroute hops from the candidate to the receiver, 7 days (`traceroute_hop`) | 78% | 99.4% | 4.7% | Repeated runs make it lean on the route truth itself; no index by node (below) |
| 7 | `map_report`: preset, firmware | 5% of nodes | | | Rule-outs only; positions already reach `node_current` |
| 8 | Survey measurements | 64 measurements, 1 session, none direct | | | Nothing to measure on live |
| 9 | Receiver's signal: as 9b, from the receiver's own direct receptions | 82% | 99.8% | 1.7% | For a survey, from survey direct receptions; untested at ground level |
| 10 | Activity: sent or reported a packet within 24 h (`node_activity_hour`, `reception`) | rule-out | | | |
| geo | Nearest within 100 km (the old `nearest`) | 99% | 80% | 76% | |
| geo | Nearest at any distance (page step 3) | 99% | 80% | 99% | |
| geo | Only candidate within 100 km / 30 km | 26% / 52% | 99.7% / 94.5% | 17% / 13% | |
| geo | Own-reach rule, floor 10 / 20 / 30 / 50 km | 54% / 51% / 48% / 42% | 99.4% / 98.4% / 98.3% / 99.8% | 17% / 16% / 18% / 18% | Unplaced candidates (30% of others) are never in reach |

Rule-outs, each tested alone: share of other candidates removed / share of true relays lost (route truth):

| Rule-out | Others removed | True relays lost |
|---|---|---|
| Radio channel differs from the receiver's (`node_current.channel`) | 55.8% | 0.6% |
| The receiver is beyond the candidate's own reach, with a 50 km floor | 60.8% | 0.2% |
| Nothing sent or reported within 24 h | 37.6% | 0.4% |
| Role `CLIENT_MUTE` | 13.1% | 0.5% |
| One hop used and the sender beyond the candidate's own reach (50 km floor) | 8.1% | 0.6% |
| Map report preset differs from the receiver's channel | 4.2% | 0.0% |
| Its own report of the packet has hops left other than the receiver's or one more | 0.6% | 0.1% |
| Map report firmware before 2.6 (no relay byte) | 0.1% | 0.0% |
| Signal over 10 dB off how the receiver / co-hearers hear it directly | 0.1% | 0.8% / 0.3% |

Together they keep a median of 2 of the 25 candidates, exactly one in 28% of receptions, and the true relay in 96.7% (97.5% without the receiver's signal check).

How much more often the true relay has a feature than the other candidates do (route truth): heard directly by the receiver 277×, a radio link to a co-hearer 103×, to the sender 49×, under 5 km from the receiver 46×, within own reach 14×, ROUTER 13×, ROUTER_LATE 2.6×, same radio channel 3.2×. Against it: CLIENT 0.40×, CLIENT_MUTE 0.03×, role unknown 0.02×, other radio channel 0.01×, inactive 0.01×.

## Base rates

| Figure | Value |
|---|---|
| Nodes per byte | mean 16.3, median 11, max 51; with a reported position mean 8.5, max 31 |
| Placed nodes with another placed node sending their byte within 1 / 5 / 10 / 20 / 50 / 100 / 200 km | 1.3% / 8.7% / 18.4% / 30.7% / 46.1% / 66.6% / 88.0% |
| Direct links, each pair's longest trusted km (2,447 pairs) | p50 4.1, p90 23.5, p95 47.9, p99 113 |
| By transmitter role, p50 / p90 / p99 km | ROUTER 11 / 77 / 196; ROUTER_LATE 6.4 / 57 / 121; CLIENT_BASE 4.6 / 21 / 111; CLIENT 3.7 / 14 / 52; CLIENT_MUTE 1.3 / 6.9 / 29 |
| Receiver to true relay, both reported (route truth) | p50 4.3, p90 26.6, p99 77.4 km |
| Nodes with a reported position / an estimate only / no place | 51.9% / 16.3% / 31.7% |
| Nodes with no trusted link with a km, so the floor is their reach | 68.8% |
| Own reach of the nodes that have one | p50 11, p90 69, p99 167 km |
| Survey direct receptions from nodes MQTT does not place | none to count (no direct measurement) |

## Where sources agree and conflict

On route truth, when both name someone and they differ:

| Sources | Both name | Differ | First right / second right |
|---|---|---|---|
| `reported` / receiver heard it directly | 48,737 | 34% | 9.5% / 89% |
| `reported` / `gateway_link` | 38,798 | 38% | 3.2% / 96% |
| `gateway_link` / receiver heard it directly | 149,554 | 5.8% | 31% / 68% |
| `gateway_link` / own-reach rule (20 km) | 67,628 | 1.1% | 28% / 71% |
| Receiver heard it directly / own-reach rule (20 km) | 127,382 | 1.6% | 100% / 0% |
| Receiver heard it directly (most) / receiver's signal | 232,319 | 1.4% | 2.4% / 97% |
| `sender_link` / `gateway_link` | 12,729 | 8.3% | 17% / 81% |
| Nearest within 100 km / own-reach rule (20 km) | 133,613 | 4.0% | 0.6% / 99% |

Agreement needs no truth, so it can be read on all traffic. On all relayed receptions the main sources agree as often as on route truth, so the route figures carry over unless sources share their errors:

| Sources | Route truth | All traffic |
|---|---|---|
| Receiver heard it directly / co-hearers' signal | 98.9% | 98.9% |
| Receiver heard it directly / `gateway_link` within reach | 97.5% | 98.3% |
| Receiver heard it directly / own-reach rule | 98.4% | 98.3% |
| Co-hearers' signal / own-reach rule | 99.2% | 99.2% |
| Receiver heard it directly / `reported` | 67.1% | 68.8% |

`reported` agrees with the others about two times in three on all traffic as well: it is about as wrong on every port as on traceroutes.

## Staleness

- Positions: of 2,869 route-truth relays (every 100th), 88% had a position of their own when heard. Their current one sits a p99 of 0.6 km from it, and 0.2% moved over 1 km. 87% reported their current position after the packet, so a position read at upload is that fresh. 1.8% of nodes moved over 25 km in the 14 days; none flew.
- Links: the receiver last heard the true relay directly a median 0.9 hours before the packet (p90 4.9, p95 15.6, p99 78 hours). Seven days keep nearly all of them; one day would lose under 5%.

## Cost

Per reception at live scale, median / p95 ms: candidates by byte 0.4 / 0.4; their positions, roles and channels 0.1 / 0.2; the packet's other receptions 0.1 / 0.1; the receiver's direct links to the candidates, 7 days, 0.7 / 1.3; the co-hearers' direct SNR of the candidates 0.2 / 3.6; own reach 0.4 / 1.0; activity 0.1 / 0.2. `radio_links` for the candidates over 24 h costs 17.6 / 31.9, and traceroute hops to the receiver 22 / 23, because `traceroute_hop` has no index by node. Upload reads `radio_links` once per batch, as before, and no traceroute hops.

## Decision: one score (2026-10-27)

Steps 2 to 4 of the 2026-10-18 decision below, the rule-outs and the ladder of naming rules, gave way to one score (`relays.py`). Solid lines are unchanged: heard directly, a survey link, and on packet pages the only candidate the gateway heard directly. "Too strong" still removes a survey candidate first.

- **Candidates**: every node sending the byte except those that cannot have sent the copy: firmware without the relay byte, or a map report preset on another spreading factor. Unplaced, CLIENT_MUTE, silent, far and other-channel nodes stay in, with terms against them. So does a node SigSleuth does not know, the unknown option, scoring `UNKNOWN_SCORE`.
- **Score**: each candidate's terms times their weights (`WEIGHTS`), summed. A softmax over the candidates and the unknown option gives each a probability.
- **Name**: the likeliest candidate, dashed, when its probability is at least 0.8 and an evidence term counts for it: within its reach (20 km floor), within 5 km, linked to a co-hearer or (after one hop) the sender, reported one hop earlier, heard directly by the receiving gateway, or a signal that matches better than an unknown node's. Without the evidence rule, role and channel let an unplaced ROUTER with nothing tying it to the copy beat the unknown option. The name is worded by the evidence family that adds most to its score; a role never words one.
- **Estimated nodes on the receiver**: no distance terms for an estimated node within 1 km of the receiver. Its estimate comes from the nodes that heard it, so its 0 km is circular. Dropping distance for every estimated node cost about 1,400 right names.
- **No name**: up to three candidates with evidence, likeliest first, on packet pages, upload replies and the coverage cell (scored there as a copy heard at the cell's centre).

Measured on a copy of live loranet.pl `msh/#`, 13–27 October, every 8th traceroute: 29,142 receptions whose route names the relay. Weights were fitted on one week and tested on the other, both ways; the shipped table is fitted on all of them. The relay is unknown for 1% of copies by assumption: route truth never has one, so each copy also counts with its true relay removed, labelled unknown, at that weight. Right / wrong, and wrong names with the true relay hidden:

| Receiver | Ladder | Score |
|---|---|---|
| Survey stand-in: the gateway with no links of its own, its report and route left out | 23,105 / 465, hidden 7,438 | 26,610 / 152, hidden 5,303 |
| Packet page: the gateway with its own direct receptions | 28,612 / 179, hidden 8,375 | 28,675 / 117, hidden 6,538 |

Calibration, packet pages where heard directly does not decide: names at 0.8–0.9 were right 81% of the time, at 0.99 or more 99.8%. Without role terms the score failed both receivers: it named fewer right and more wrong than the ladder. With them, distance would otherwise word nearly every name. Without the unknown option, wrong names with the true relay hidden rise two to three times. A hearing-rate term (how often each co-hearer hears the candidate) added nothing on ordinary copies at a 1% unknown share and cost 290 ms per busy page; not used.

What the weights say: distance falls steeply to about 50 km, then flattens (`log_km`, `beyond_reach`, `receiver_far`); being within reach and links count for a candidate; reporting the packet with other hops left, not being active within a day, an unplaced node and CLIENT_MUTE count strongly against it; ROUTER counts for it, as route truth sits on routers' paths. With no role or links, a lone candidate is named only within about 10 km.

Weights are per mesh. To refit on another network, or after a firmware change: `uv run --locked --with numpy python scripts/relay-fit.py DATABASE`. It prints out-of-fold figures for the refit and the current table, then a table to paste into `relays.py`. Packet pages score at read time, and upload names are saved facts, so no derived table needs a rebuild.

Revisit at ground level: surveys have no route truth yet, and route truth favours routers.

## Decision (2026-10-18)

Superseded for naming by the score above, 2026-10-27; solid lines are as decided here.

Solid lines need direct evidence; dashed guesses can also use the co-hearers' signal score (below). Upload (`coverage.heard_node`) and survey links (`coverage.refresh_survey_links`):

1. **Solid**: a survey heard the node directly. The measurement had no relay, or the relay byte matches exactly one node a survey heard directly nearby (a survey link: any survey node, same radio settings, 30 km, closest of 1 hour or 60 days). Checked before the rule-outs, so none can overrule a direct reception, and it wins over upload's name.
2. **Rule-outs**: every row of the rule-out table except the two signal rows. Unplaced nodes are excluded from guesses. Survey links (before naming) and upload also leave out candidates that arrived too strong (below).
3. **Dashed**: of the candidates left, the first rule that names one: the co-hearer signal score; then `gateway_link`, `sender_link` and `reported`, each only within the candidate's own reach (20 km floor); then the only candidate left, also only within its reach (since 2026-10-23, below).
4. **No line** otherwise. The cell lists up to three candidates that pass the rule-outs needing no packet (role, spreading factor, firmware, reach), nearest first.
5. A measurement with a line, solid or dashed, paints while that node is alive; one without paints for 14 days.

Packet pages and the chat's gateway reports (`packets._with_relays`) take the same steps with the gateway in the phone's place. Solid is the only candidate the gateway heard directly within 7 days of the packet, before the rule-outs: the study's stand-in for survey links, so the "with survey links" figures below are the page's own. Before, the page named someone in 99.1% of route receptions, 97.0% right, and in 98.9% with the true relay hidden. A reception that names no one lists up to three candidates the rule-outs left, nearest first, and one carrying the gateway's own byte names no one. Direct counts and radio links count either side of the packet, where upload has only those before: retention removes link hours at the same age as packets. The signal score uses only completed hours before the packet on both pages and uploads; without a profile, the other rules still apply.

Since 2026-10-26, reach also counts trusted outgoing traceroute hops with SNR, even when shorter direct or neighbour links exist. A short gateway link does not limit the radio's demonstrated reach. Unplaced nodes are excluded from guesses and candidate lists; direct survey or gateway evidence still wins before those rule-outs. Several candidates leave the relay unnamed unless stronger evidence chooses one, and the remaining candidates are still listed.

Since 2026-10-27, `sender_link` also counts traceroute hops with a measured SNR between the sender and a candidate, either way, over the same day as radio links. A router that no gateway hears directly is often seen hearing the sender only in traceroutes. Measured on every third traceroute of a copy of live loranet.pl (2026-10-13 to 10-26: 12,019 receptions after one hop whose route names the relay, without the receiver's direct links, hops from the day before each packet only so that a route cannot name its own relay): right 88.4% → 92.0%, wrong 1.82% → 1.76%, wrong with the true relay hidden 34.2% → 34.5%. With the receiver's direct links first, as packet pages do (every ninth): right 97.1% → 98.1%. Hops from the sender only, or traceroutes only when no radio link exists, did slightly worse. Most new wrong names are 0-cost hops: the last relay took a router's copy for free and never heard the sender. `gateway_link` still reads radio links only: traceroute counts in both steps raised wrong names with the true relay hidden from 15.3% to 16.1% (2026-10-26), the sender step alone by 0.3 points.

The next two tables describe the 2026-10-18 decision, before the reach check and signal score. On route truth, with the receiver's direct links standing in for survey links or without them:

| | Before | Decided |
|---|---|---|
| Solid lines | 19.1%, 65.8% right | 85.4%, 99.8% right |
| Dashed lines, with survey links | 75.7%, 97.1% right | 9.8%, 88.7% right |
| Dashed lines, without | 43.1%, 95.0% right | 72.0%, 95.2% right |
| No line, with / without | 5.2% / 37.7% | 4.7% / 28.0% |
| Named, so painting while its node lives, with / without | 94.8% / 62.3% | 95.3% / 72.0% |
| Named node right, with / without | 90.8% / 86.0% | 98.6% / 95.2% |
| Wrong lines with the true relay hidden, with / without | 19.7% / 15.9% | 35.4% / 31.6% |

Each step at its turn, with survey links / without: names someone, right when it does, wrong lines with the true relay hidden:

| Step | Line | With survey links | Without |
|---|---|---|---|
| Survey link | solid | 85.4%, 99.8%; 6.4% | |
| 2d: co-hearers heard it directly | dashed | 5.3%, 98.0%; 3.6% | 48.0%, 99.0%; 4.5% |
| `gateway_link`, within reach | dashed | 2.6%, 72.8%; 1.8% | 6.6%, 79.1%; 2.6% |
| `sender_link`, within reach | dashed | 0.1%, 77.7%; 0.2% | 1.3%, 91.0%; 0.3% |
| `reported`, within reach | dashed | 0.9%, 88.9%; 1.1% | 4.4%, 87.4%; 1.3% |
| Only candidate left | dashed | 0.8%, 80.6%; 22.2% | 11.8%, 92.5%; 22.9% |

Before the reach check below, the only candidate left named another node in 22% of receptions when the true relay was hidden. On MQTT that is rare: 22 of the 901 relays traceroutes name (2.4%) never sent a packet MQTT saw, 0.1% of route receptions. Nodes that turn OK to MQTT off, or relay where no gateway listens, are the risk, accepted.

Since 2026-10-23 that step also names only within the candidate's own reach (20 km floor), like the steps before it: a far candidate left alone mostly stands in for a relay SigSleuth does not know. On every 10th traceroute of the same data (30,012 receptions with route truth), without survey links: wrong lines with the true relay hidden 31.7% → 15.8%, named node right 95.1% → 95.7%, named 72.1% → 71.3%; with them, wrong lines with the true relay hidden 35.4% → 19.7%. A later live copy (2026-10-09 to 10-23, every 20th traceroute, 23,110) agrees: 30.8% → 15.4%, right 95.6% → 96.0%, named 70.0% → 69.4%. The tables above are from before the change.

### Co-hearer signal score (2026-10-24)

The score replaces 2d. It compares the same copy's SNR at each co-hearer with that gateway's direct readings of each candidate's own packets. Profiles use the seven completed days before the packet's hour, so the packet cannot enter its own profile. A gateway uploading its own rebroadcast supplies no signal evidence. Missing readings leave only the direct-contact evidence; missing profiles remain possible. The best candidate must beat both the next candidate and an unknown node. Otherwise the link and report rules still run.

Measured 2026-10-23 on two live loranet.pl snapshots, 2026-09-27 to 10-12 and 10-09 to 10-23: a fixed 2.6 dB spread, 31 dB unknown width and 3-nat margin reduce wrong names beyond the reach check from 919 to 570 and 640 to 294, with nearly unchanged naming coverage. Right when named rises from 95.7% to 97.3% and 96.0% to 98.2%. The receiver's direct links standing in for survey links give 99.4% right on both. These are samples of 30,012 and 23,110 route-truth receptions, including opted-out candidates; multiple receptions can come from one packet.

The gain comes mostly from choosing between candidates before the weaker radio-link fallback does. On the newer snapshot, 365 wrong names become right, but 20 right names become wrong. Wrong names with the true relay hidden barely change. This is still a dashed guess, classified as `gateway_link`; a signal match never overrules direct evidence. The later dates support the gain, but moving phones and another mesh remain untested. Revisit the spread and margin there.

## Too strong (2026-10-22)

A relayed copy is sent at the relay's own power over its own path, so it cannot arrive much stronger than that node's own packets heard from the same place. Weaker is normal (a pocket, indoors, fading). The signal is `rx_rssi + min(rx_snr, 0)`: RSSI reads the noise below 0 dB SNR, and SNR alone saturates near +12 dB. An RSSI of 0 is missing.

Measured on a copy of live loranet.pl `msh/#`, 2026-09-27 to 2026-10-12, by `scripts/relay-signal.py DATABASE` (about 20 seconds), with the gateway in the phone's place: 278,899 route-truth receptions with an RSSI, each candidate checked against the gateway's own direct receptions of it in the 30 days before. A candidate is ruled out when the copy is over the margin above the strongest of them:

| Margin | True relays ruled out | Wrong candidates heard directly, ruled out |
|---|---|---|
| 10 dB | 0.17% | 48% |
| 20 dB | 0.09% | 30% |
| 30 dB | 0.035% | 15% |
| 40 dB | 0% | 6% |

- By reading, at 20 dB with 3 or more readings: the signal 0.086% / 28.0%, RSSI alone 0.025% / 12.6%, SNR alone 0.005% / 0.6%.
- Where exactly one candidate was heard directly (the survey-link stand-in), 20 dB cuts wrong names from 519 to 394 and names 2.6% more right ones, since it also settles pairs; 30 dB, 519 to 468 and 1.3% more.
- A "too weak" check against the weakest reading is as safe on gateways (20 dB: 0.021% / 7.3%), but a phone loses signal in pockets and indoors: not used.

Decided: survey links and upload leave out a candidate whose copy is over 30 dB above the strongest signal at which the same survey node heard it directly within 1 km, 60 days either side. The same survey node, because radios read RSSI differently (an LNA); 30 dB, not 20, because a moving phone varies more within 1 km than a fixed gateway. A candidate without such a reading is not checked: a node heard weakly far away may be strong here, and reported positions cannot set a ceiling (about 1 in 10 placed node pairs 10 km or more apart arrive stronger than free-space loss allows). Packet pages could use it with the gateway in the phone's place, but `link_hour` keeps no RSSI.

### Wording

Every relayed name stays a guess unless it is solid. Since 2026-10-27 a guess is worded by the evidence that adds most to its score.

| Evidence | Line | Wording |
|---|---|---|
| Heard directly | solid | Heard directly |
| Gateway heard it directly (packet page) | no "≈" | Heard directly: the only node ending in 1a this gateway heard directly within a week of the packet |
| Survey link | solid | Heard directly nearby: the only node ending in 1a a survey heard |
| Gateway heard it and others directly (packet page, `receiver_heard`) | dashed | Guess: likeliest node ending in 1a this gateway hears |
| Links with a co-hearer or its signal, `gateway_link` | dashed | Guess: linked to a gateway that heard the same copy |
| `sender_link` | dashed | Guess: linked to the sender |
| `reported` | dashed | Guess: it relayed the packet (MQTT), maybe not the copy heard here |
| Distance, `only_candidate` | dashed | Guess: the likeliest node ending in 1a nearby |
| None | none | Unknown relay ending in 1a; Maybe X (3.2 km), Y (12 km), likeliest first |

## Routes between packet relays (2026-10-23)

The packet page's relay map shows identified relays and likely routes between them. It draws whom each relay heard the packet from (`flood_routes.py`), walking back from every relay a gateway heard after k hops:

1. The relay's own report, when it is a gateway that heard the packet after k − 1 hops, or k (a 0-cost hop: a favourite router relays without spending a hop). Right 94.4% after 2+ hops, 99.5% after one.
2. Else after one hop, for a router relay never heard hearing the sender: the one node also sent after one hop whose copies it passes on for free (below). When free partners are known but none or several sent after one hop, leave a gap.
3. Else after one hop, the sender: right 87%; the rest are 0-cost hops.
4. Else a score over the relay's candidates: its radio partners within 7 days either side (direct, neighbour or traceroute links either way), the nodes this packet's reports put a hop earlier or at the same hop, and the node its own report names. The true node was among them 99.3% of the time. A softmax over them gives each a probability; the best is drawn only when at least 0.8.

The walk goes on from each node found until the sender, and stops at its first unsure hop, leaving a gap. When the best candidate opted out, it stops rather than take the next one. A node found over a 0-cost hop keeps the relay's level: its copy was heard after k hops and not k − 1, or the relay's own report names it and the relay heard the packet after k. Going on from it at k − 1 instead drew a false "one hop, so the sender" line at the end; keeping its level cut wrong hops from 1,164 to 961 of about 10,600 drawn and lost no right one (2,135 traceroute requests with 3+ receptions, a copy of live loranet.pl, 2026-10-16 to 10-23, every third).

Only ROUTER, ROUTER_LATE and CLIENT_BASE relays keep the hop count, from firmware 2.7.11, and only for a favourite router's copy. Traceroutes show which pairs do (`hop_cost_hour`): a request copy heard after one hop over k ≥ 2 relays was free on every hop after the first relay, one heard after k hops spent a hop on each. Each packet counts once, per direction and channel; the first hop always costs and counts nothing. A router relay named after one hop with no report of its own, never heard hearing the sender (no direct, neighbour or traceroute link from it within 7 days either side), and with exactly one learned partner among the nodes sent after one hop, gets that partner as predecessor at step 1, dashed, and the walk goes on from it. A partner counts when free at least as often as costly on the packet's channel: from firmware 2.8 a relay that cannot decrypt a packet always spends a hop. Several partners leave a gap. On the same requests, before rejecting mutual guesses and with each request's own hops and hop costs left out, wrong hops fell from 961 to 840 and right ones rose by 93 (99 gained, 6 lost). One-hop lines went from 1,397 at 83.5% right to 1,248 at 92.9%, plus 121 0-cost lines at 81.8%. 14 days learned 8,502 pairs (by direction and channel) from 14,782 requests; 185 are free at least as often as not. In every 20th broadcast of the second week, 977 lines in 6% of broadcasts are 0-cost; ignoring the channel would change 5 of 13,095 relays named after one hop, all on one private channel. Mutual guessed partners leave gaps; a gateway's report can resolve the direction. Before this check, 13 of the 2,135 requests drew both directions for such a pair, one of the two wrong.

| Candidate X for relay T | Weight |
|---|---|
| T's own report names X | +4.166 |
| X was a named relay one hop earlier | +3.311 |
| T heard X in a traceroute | +2.309 |
| ln(1 + traceroute hops X → T) | +1.053 |
| Progress: (km X–sender − km T–sender) / km T–sender, within ±2 | −0.957 |
| This packet's reports put X at another hop | −4.262 |

Measured on a copy of live loranet.pl `msh/#`, 2026-10-09 to 10-23, with traceroute request routes as truth; weights fit on the first week, figures from the second (26,047 relays heard after 2+ hops; traceroutes the sender ran or was traced in left out). The best candidate is right 68.5% of the time. It is at least 0.8 sure for 49% of hops and then right 91.9% (at 0.9: 38%, 94.8%). Without traceroute links it is right 49.6%, sure for a fifth. Simple rules (most traceroute hops into the relay; the relay named one hop earlier) reach at most 55%. Whole routes with every hop sure cover 18% of receptions after 2+ hops, 95% of them within 5 km of the true route. Checked on 300 second-week requests heard by 8+ gateways, with only each request's own run left out: 90.4% of 1,025 sure hops right.

Unlike the relay ladder's checkable steps, this is a score: where it is wrong, the true node is mostly its second choice, another router the relay hears, and which copy came first is near chance from what MQTT keeps. So only the sure hops are drawn, as thin grey lines, dashed unless the relay's own report heard the node directly. They cost 140–160 ms beyond the packet page for broadcasts heard by 60+ gateways, so they load only when the relay map opens.

Revisit the weights and the 0.8 on another network, or when firmware changes how floods are relayed.

## Not measured yet

- **Survey links** (30 km, 60 days, any survey node): the stand-in reads one fixed gateway's 7 days. Settled by surveys that hear traceroutes: the phone uploading a traceroute's route would give survey measurements route truth.
- **Relays MQTT never sees**: hidden figures assume the worst case for each reception; on gateways such relays are 2.4% of relays and 0.1% of receptions, likely more where no gateway listens. Settled by counting survey relay bytes that match no node in `node_current`, and survey direct receptions from nodes MQTT does not place. Live has no direct survey measurement.
- **Phones at ground level**: shorter, more varied paths; a moving phone's SNR from one relay spreads more than a gateway's. The signal score needs survey direct receptions repeated in one place.
- **Signal at ground level**: the "too strong" rule's 30 dB is measured on gateways (above); how much a phone's signal from one relay varies within 1 km is untested. `link_hour` keeps SNR only, so packet pages can't use it yet.
