# Glossary

Terms the code and pages use, in plain words. Rules and thresholds live in the code named beside them.

## Capture

| Term | Meaning |
|---|---|
| Broker / subscription | The MQTT server gateways upload to / the topic filter SigSleuth listens to, which may span several networks. |
| Topic root | The part of a topic before `/2/e/<channel>/<gateway>`, chosen by each gateway. One mesh often uploads under several, so it is never part of node or packet identity. |
| Delivery | One MQTT message. Capture saves it, or skips it with a reason shown on Diagnostics. |
| Reception (gateway report) | One gateway's report of a packet. |
| Copies | Repeated MQTT deliveries of the same reception; they add to its copy count, not to receptions. |
| Packet | One radio message, found by grouping its receptions (`ingest.group_reception`). Its row ID is SigSleuth's; its packet ID is the sender's 32-bit number, which can repeat. |
| Inactivity window | A matching reception joins a packet if it arrives within 30 minutes of the packet's latest one. |
| Map report | A node's own MQTT self-description on `<root>/2/map/`, about hourly. Not a packet; names and places a node only until its own NodeInfo and position arrive. |
| Facts / derived tables | What was reported, as saved (`schema.sql`), including each node's names, positions and OK to MQTT setting, which outlive its packets (`reported.py`) / tables computed from facts for fast pages (`derived.sql`, `derive.py`). |
| Rederive | Rebuilding the derived tables from facts after `DERIVED_VERSION` changes. |
| Default channel key | The public Meshtastic key; the only one SigSleuth decrypts with. |
| Sentinel | A value meaning "unavailable", such as an SNR of 0 for a gateway's own packet. Saved as missing. |

## Nodes and positions

| Term | Meaning |
|---|---|
| Node | A radio's node number, such as `!aabbccdd`. It does not prove one physical device: numbers can collide. |
| Gateway | The node that uploaded a reception to MQTT. It may not be a node SigSleuth has heard itself. |
| NodeInfo | A node's broadcast of its names, hardware and role. |
| Opted-out node | One whose newest OK to MQTT setting, from a captured packet or a survey, was off, even once that packet has expired; a node reporting to MQTT as a gateway never is (`opt_out.py`). Its history is kept, but no page names it: its own node, the packets to or from it and its links are left out (another node's packet to it keeps its time, without a link), and other nodes' routes, neighbour lists and relays show it as unknown. Its survey measurements read as an unknown relay's ([coverage](coverage.md#nodes-that-do-not-allow-mqtt)). Turning the setting on shows its whole history again. With `SIGSLEUTH_HIDE_OPTED_OUT=false`, no node is opted out. |
| Last sent / last seen | When the node last originated a packet / that, or when it last reported as a gateway or appeared in a traceroute hop. |
| Valid position | Coordinates in range and not near 0,0. An invalid one never replaces the latest valid one. |
| Estimated position | Where a node that reports no position probably is, from its placed radio neighbours (`positions.py`). Drawn hollow; never used for km. |
| Doubtful position | A reported position the node's own direct links contradict (`positions.py`). Its links are hidden on the map and badged elsewhere. |
| Moved node | A node whose new position is over 25 km from the last; its earlier link hours are hidden on the map. |
| Aircraft | A node heard directly in two far-apart places at once (`derive._mark_airborne`). |
| Unlikely long link | A link 250 km or longer that the terrain check rejects. |
| Pending distance | A distance of 100 km or more ranked apart until both ends have reported a position after it was heard, without either moving since. If an end moved after it was heard, that historical distance stays pending even after further reports. |
| Distance when heard | km between the nodes' reported positions when the link was last heard (`reported.positions_when_heard`). Link pages show only this; the map draws nodes where they are now. |
| Channel | The channel name a packet was uploaded under. `PKI` means private messages, not a channel. |
| Radio channel | A channel name that tells the radio settings (`radio.py`). A node's channel is the radio channel of its latest packet on one. |
| Packet channel | The number on one packet: a channel hash when encrypted, a gateway-local index when decoded. |
| Firmware estimate | The oldest firmware a node's packets prove (`firmware.py`), shown when it sends no map report. A minimum, not the version. |

## Radio

| Term | Meaning |
|---|---|
| RSSI / SNR | Signal strength / signal-to-noise ratio as the receiver measured it. Not delivery success. |
| Hop start / hop limit | Header counters; their difference is hops travelled. Equal values alone do not prove direct reception. |
| Relay byte | The last byte of the relaying node's number. Usually matches many nodes; pages name one by the [relay evidence](relay-evidence.md) rules, marked "≈" unless the gateway heard it directly. |
| Spreading factor | LoRa setting fixed by the modem preset, guessed from the channel name. |
| Fade margin | How far an SNR is above the weakest decodable at its spreading factor. Links are rated by it. |
| ChUtil / AirUtil | A node's reported channel busy share / own transmit share. |

## Traceroutes and links

| Term | Meaning |
|---|---|
| Traceroute | A request from A to B that records each relay and SNR; B's reply carries the route back. Each gateway reports a different version. |
| Route | One direction of a traceroute as one gateway reported it ("route to target", "route back"). |
| Traceroute run | A request and its reply, matched by requester and request packet ID. |
| Reached | A route known to have reached its end (`traceroute.reached_end`); others are "not confirmed", not failed. |
| Hop | One step of a route: X transmitted, Y heard it. |
| Direct reception | A reception where the gateway heard the sender with no relay, by a strict rule (`derive.is_direct_reception`) that misses some. |
| Neighbour info | A node's list of up to 10 nodes it heard directly in the last few hours. |
| Link | A node pair with radio evidence in the window, per kind (traceroute, direct, neighbour) and per channel, never added together. |
| Text reach | The share of a sender's broadcast texts each gateway reported. Approximate. |
| Line of sight | Whether terrain leaves the path between two antennas open (60% of the Fresnel zone clear). Rough: no buildings. |

## Zones

| Term | Meaning |
|---|---|
| Heard link / solid link | A traceroute link heard at least twice with an SNR / one heard both ways, often, with margin to spare. |
| Zone | Nodes that link to each other more than to the rest (Louvain on heard links), named after a nearby town or its hub. |
| Hub | A zone's most-linked member. |
| Cuts off / no solid backup | Nodes that lose every known path / every solid path if a relay goes quiet. |
| Island | Zones joined by heard links, directly or not. |
| Group | Zones joined box by box: two boxes join when at least the set number of solid links run between them. |
| Weak connection | The links between two zones in different groups. |
| Single link | A zone pair whose solid links are the only solid route between two parts of the network. |
| Alternative path | The shortest solid path between two zones that avoids their direct links. |
| Text reach between zones | How often broadcast texts sent in one zone were reported by a gateway in another. |
| Carries | How many nodes' best path to a big zone's hub passes through a relay. |

## Coverage

| Term | Meaning |
|---|---|
| Signal survey | A phone app ([Mesh Survey](https://github.com/joroses/mesh-survey), an Android fork) recording every packet its node hears, with the phone's position, and uploading them. |
| Measurement | One packet the survey node heard. Not a reception. |
| Heard node | The node the survey node actually received: the sender if direct, else the last relay, often guessed from the relay byte ([relay evidence](relay-evidence.md)). The map's line to it is solid when it was heard directly or by a survey link, dashed when guessed. |
| Survey link | A heard node named from a direct reception: the one node sending that relay byte that a survey node heard directly nearby around then (`coverage.survey_candidates`). It wins over upload's guess. |
| Own reach | A node's longest trusted link it transmitted on (`positions.own_reach`). A relay candidate farther than it and 50 km is ruled out; one is named only within it or 20 km. |
| Only candidate | A heard node guessed because the rule-outs left no other node sending its relay byte (`coverage.heard_node`). |
| Still-true measurement | One the coverage map counts: not heard before its node moved or flew, and recent or of a node seen since ([coverage](coverage.md)). |
| Coverage cell | A square of the map's coverage grid, coloured by average fade margin. |
