-- Derived tables: what pages need fast, computed from the facts (schema.sql)
-- by derive.py during capture. To change one, raise DERIVED_VERSION in db.py:
-- startup recreates them empty and rederives saved history. No upgrade code.

-- The derived tables' version, and whether rederive finished (rebuilt).
CREATE TABLE derived_state (
    version INTEGER NOT NULL,
    rebuilt INTEGER NOT NULL CHECK (rebuilt IN (0, 1))
) STRICT;

-- Every node seen in retained history: as sender, gateway, traceroute hop or
-- map report. Last sent, NodeInfo and position are copied from what it
-- reported (node_reported, its newest node_position row), which outlives
-- the packets; a map report's names stand in until a NodeInfo arrives.
-- The other parts (channel, telemetry, firmware) only move to newer data.
-- channel is the latest packet's channel among radio channels (radio.py);
-- PKI and other channels go out on the same radio, so they never change it.
-- last_seen_at also counts gateway reports and traceroute hops, so it can be
-- newer than last_sent_at.
-- firmware_minimum is major * 10000 + minor * 100 + patch, proven by
-- firmware_trait (firmware.TRAITS); it only moves up.
-- moved_at: last move over derive.MOVED_KM between node_position rows; pages
-- hide link hours before it. The packet IDs may name deleted packets.
CREATE TABLE node_current (
    node_id INTEGER PRIMARY KEY CHECK (node_id BETWEEN 1 AND 4294967295),
    last_sent_at INTEGER,
    last_sent_packet_row_id INTEGER,
    last_seen_at INTEGER NOT NULL,
    last_gateway_at INTEGER,
    node_info_observed_at INTEGER,
    node_info_packet_row_id INTEGER,
    long_name TEXT CHECK (long_name IS NULL OR length(CAST(long_name AS BLOB)) <= 1024),
    short_name TEXT CHECK (short_name IS NULL OR length(CAST(short_name AS BLOB)) <= 1024),
    hardware_model TEXT CHECK (hardware_model IS NULL OR length(hardware_model) <= 64),
    role TEXT CHECK (role IS NULL OR length(role) <= 64),
    position_observed_at INTEGER,
    position_packet_row_id INTEGER,
    latitude REAL,
    longitude REAL,
    altitude_meters INTEGER,
    device_time INTEGER,
    utilization_observed_at INTEGER,
    channel_utilization REAL,
    air_util_tx REAL,
    battery_observed_at INTEGER,
    battery_level INTEGER CHECK (battery_level IS NULL OR battery_level BETWEEN 1 AND 101),
    firmware_minimum INTEGER,
    firmware_trait TEXT CHECK (firmware_trait IS NULL OR length(firmware_trait) <= 64),
    channel TEXT,
    channel_observed_at INTEGER,
    moved_at INTEGER,
    CHECK ((last_sent_at IS NULL) = (last_sent_packet_row_id IS NULL)),
    CHECK ((channel IS NULL) = (channel_observed_at IS NULL)),
    CHECK ((node_info_observed_at IS NULL) = (node_info_packet_row_id IS NULL)),
    CHECK (position_packet_row_id IS NULL OR position_observed_at IS NOT NULL),
    CHECK (position_observed_at IS NOT NULL OR (latitude IS NULL AND longitude IS NULL
        AND altitude_meters IS NULL AND device_time IS NULL)),
    CHECK (position_observed_at IS NULL OR (latitude IS NOT NULL AND longitude IS NOT NULL))
) STRICT;

-- The node a relayed survey measurement most likely heard, from survey nodes'
-- direct receptions (coverage.refresh_survey_links); it wins over upload's
-- name. Refreshed around each upload's measurements and wholly by rederive.
CREATE TABLE coverage_relay (
    measurement_id INTEGER PRIMARY KEY REFERENCES coverage_measurement(id) ON DELETE CASCADE,
    node_id INTEGER NOT NULL CHECK (node_id BETWEEN 1 AND 4294967295)
) STRICT;

CREATE INDEX coverage_relay_node ON coverage_relay (node_id);

-- Nodes that turned OK to MQTT off, which no page names (opt_out.py).
-- Refreshed for a sender when one of its packets or survey measurements
-- tells its bit, after survey retention, and wholly by rederive; a node
-- reporting as a gateway leaves it.
CREATE TABLE node_opt_out (
    node_id INTEGER PRIMARY KEY CHECK (node_id BETWEEN 1 AND 4294967295)
) STRICT;

-- Estimated positions of linked nodes that report none (positions.py),
-- refilled wholesale by refresh_estimates. A reported position always wins.
CREATE TABLE node_estimate (
    node_id INTEGER PRIMARY KEY CHECK (node_id BETWEEN 1 AND 4294967295),
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    neighbours INTEGER NOT NULL CHECK (neighbours >= 1),
    spread_km REAL NOT NULL,
    from_traceroute INTEGER NOT NULL CHECK (from_traceroute IN (0, 1))
) STRICT;

-- Nodes whose own direct links contradict their reported position
-- (positions.doubt_limit_km), refilled with node_estimate.
CREATE TABLE node_doubtful (
    node_id INTEGER PRIMARY KEY CHECK (node_id BETWEEN 1 AND 4294967295),
    partners INTEGER NOT NULL CHECK (partners >= 1),
    median_km REAL NOT NULL
) STRICT;

-- Terrain verdict per pair heard links.LONG_LINK_KM apart or more, every
-- link kind and channel together, node_a < node_b (terrain.py).
-- Judged once while the pair's longest km stays the same; no row means not
-- judged yet (tiles unavailable), and the link is shown unchecked.
CREATE TABLE long_link (
    node_a INTEGER NOT NULL,
    node_b INTEGER NOT NULL,
    km REAL NOT NULL,
    sea_share REAL NOT NULL CHECK (sea_share BETWEEN 0 AND 1),
    above_meters REAL NOT NULL,
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
    unlikely INTEGER NOT NULL CHECK (unlikely IN (0, 1)),
    PRIMARY KEY (node_a, node_b)
) STRICT, WITHOUT ROWID;

-- One row per hop per traceroute packet: from_node_id transmitted and
-- to_node_id heard it. Later gateways repeating the hop add nothing, so a row
-- is one observation. The route_ columns are the packet's best route with the
-- hop (as in traceroute_packet), for the Traceroute explorer's node search.
-- free is whether the first request copy to pin the hop's cost
-- (traceroute.pinned_costs) found it free, NULL until one does; it counts the
-- hop once in hop_cost_hour.
CREATE TABLE traceroute_hop (
    packet_row_id INTEGER NOT NULL REFERENCES packet(id) ON DELETE CASCADE,
    from_node_id INTEGER NOT NULL CHECK (from_node_id BETWEEN 0 AND 4294967295),
    to_node_id INTEGER NOT NULL CHECK (to_node_id BETWEEN 0 AND 4294967295),
    snr_db REAL,
    route_reception_id INTEGER NOT NULL,
    route_is_return INTEGER NOT NULL CHECK (route_is_return IN (0, 1)),
    route_hops INTEGER NOT NULL CHECK (route_hops >= 0),
    route_km REAL,
    route_reached INTEGER NOT NULL CHECK (route_reached IN (0, 1)),
    free INTEGER CHECK (free IN (0, 1)),
    PRIMARY KEY (packet_row_id, from_node_id, to_node_id)
) STRICT, WITHOUT ROWID;

-- One row per readable traceroute packet, for the Traceroute explorer: its
-- longest route so far and its route that reached its end (success_route_,
-- traceroute.successful_route). A run is (requester_node_id,
-- request_packet_id). km figures use where the nodes were when heard
-- (reported.positions_when_heard).
CREATE TABLE traceroute_packet (
    packet_row_id INTEGER PRIMARY KEY REFERENCES packet(id) ON DELETE CASCADE,
    requester_node_id INTEGER NOT NULL CHECK (requester_node_id BETWEEN 0 AND 4294967295),
    target_node_id INTEGER NOT NULL CHECK (target_node_id BETWEEN 0 AND 4294967295),
    request_packet_id INTEGER NOT NULL,  -- a request's packet_id, a reply's request_id
    is_reply INTEGER NOT NULL CHECK (is_reply IN (0, 1)),
    route_reception_id INTEGER NOT NULL,
    route_is_return INTEGER NOT NULL CHECK (route_is_return IN (0, 1)),
    route_hops INTEGER NOT NULL CHECK (route_hops >= 0),
    route_km REAL,
    success_route_reception_id INTEGER,
    success_route_hops INTEGER CHECK (success_route_hops >= 0),
    success_route_km REAL,
    ends_km REAL
) STRICT;
CREATE INDEX traceroute_packet_run ON traceroute_packet (requester_node_id, request_packet_id);
-- For the Traceroute explorer's node search and a link's traceroute packets.
CREATE INDEX traceroute_hop_from ON traceroute_hop (from_node_id, to_node_id);
CREATE INDEX traceroute_hop_to ON traceroute_hop (to_node_id);

-- One row per traceroute run, for the Traceroute explorer: the route it
-- shows (route_, the packet, reception and direction whose report holds it)
-- is the route that worked from its earliest reply with one, else from its
-- earliest request with one (reached), otherwise its longest route. ends_
-- is its first packet with an ends_km. A run is in a window, and is deleted,
-- with its first packet.
CREATE TABLE traceroute_run (
    requester_node_id INTEGER NOT NULL,
    request_packet_id INTEGER NOT NULL,
    target_node_id INTEGER NOT NULL,  -- the first packet's
    first_packet_row_id INTEGER NOT NULL REFERENCES packet(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,  -- the first packet's
    reached INTEGER NOT NULL CHECK (reached IN (0, 1)),
    route_packet_row_id INTEGER NOT NULL,
    route_reception_id INTEGER NOT NULL,
    route_is_return INTEGER NOT NULL CHECK (route_is_return IN (0, 1)),
    route_hops INTEGER NOT NULL CHECK (route_hops >= 0),
    route_km REAL,
    ends_packet_row_id INTEGER,
    ends_km REAL,
    PRIMARY KEY (requester_node_id, request_packet_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX traceroute_run_first ON traceroute_run (first_packet_row_id);

-- Hourly link observations per direction, kind and channel: from_node_id
-- was heard by to_node_id. Kinds and channels are never added together: a
-- channel's radio settings decide what an SNR means.
--   'traceroute': a traceroute_hop, once per packet.
--   'direct': derive.is_direct_reception, once per reception.
--   'neighbor': a NeighborInfo listing (decode.read_neighbors), once per
--     NeighborInfo packet. A neighbour stays listed for hours, so this counts
--     reports, not radio packets.
-- km is between where the nodes were at the hour's latest observation
-- (reported.positions_when_heard); NULL while either was unplaced.
CREATE TABLE link_hour (
    link_kind TEXT NOT NULL CHECK (link_kind IN ('traceroute', 'direct', 'neighbor')),
    hour_start INTEGER NOT NULL,
    from_node_id INTEGER NOT NULL CHECK (from_node_id BETWEEN 0 AND 4294967295),
    to_node_id INTEGER NOT NULL CHECK (to_node_id BETWEEN 0 AND 4294967295),
    observations INTEGER NOT NULL CHECK (observations >= 1),
    snr_count INTEGER NOT NULL CHECK (snr_count BETWEEN 0 AND observations),
    snr_sum REAL NOT NULL,
    last_seen_at INTEGER NOT NULL,
    channel TEXT NOT NULL CHECK (length(CAST(channel AS BLOB)) BETWEEN 1 AND 1024),
    km REAL CHECK (km IS NULL OR km >= 0),
    PRIMARY KEY (link_kind, hour_start, from_node_id, to_node_id, channel)
) STRICT, WITHOUT ROWID;
-- Also gives each direction's longest km without reading the table.
CREATE INDEX link_hour_pair ON link_hour (from_node_id, to_node_id, km);

-- Hourly counts of traceroute request hops whose cost a single copy pins
-- (traceroute.pinned_costs), per direction and channel, once per packet:
-- free when to_node_id passed on from_node_id's copy without spending a hop
-- (from firmware 2.7.11, a router role relaying a favourite router), costly
-- when it spent one. It holds for that channel only: from 2.8 a relay that
-- cannot decrypt a packet always spends a hop. Read for the packet
-- relays after one hop (flood_routes.py) and a node's links.
CREATE TABLE hop_cost_hour (
    hour_start INTEGER NOT NULL,
    from_node_id INTEGER NOT NULL CHECK (from_node_id BETWEEN 0 AND 4294967295),
    to_node_id INTEGER NOT NULL CHECK (to_node_id BETWEEN 0 AND 4294967295),
    channel TEXT NOT NULL CHECK (length(CAST(channel AS BLOB)) BETWEEN 1 AND 1024),
    free INTEGER NOT NULL CHECK (free >= 0),
    costly INTEGER NOT NULL CHECK (costly >= 0),
    PRIMARY KEY (hour_start, from_node_id, to_node_id, channel)
) STRICT, WITHOUT ROWID;
CREATE INDEX hop_cost_hour_to ON hop_cost_hour (to_node_id, free, costly);
CREATE INDEX hop_cost_hour_from ON hop_cost_hour (from_node_id, free, costly);

-- Hourly counts of each node's own packets by port and broadcast or not.
-- portnum is -1 until a report is readable; then the count moves to its port.
CREATE TABLE node_activity_hour (
    hour_start INTEGER NOT NULL,
    node_id INTEGER NOT NULL CHECK (node_id BETWEEN 1 AND 4294967295),
    portnum INTEGER NOT NULL CHECK (portnum BETWEEN -1 AND 4294967295),
    is_broadcast INTEGER NOT NULL CHECK (is_broadcast IN (0, 1)),
    packets INTEGER NOT NULL CHECK (packets >= 0),
    last_sent_at INTEGER NOT NULL,
    PRIMARY KEY (hour_start, node_id, portnum, is_broadcast)
) STRICT, WITHOUT ROWID;

CREATE INDEX node_activity_node ON node_activity_hour (node_id, hour_start);

-- Covers every column Chatty nodes reads, so the table is not visited.
CREATE INDEX node_activity_text
ON node_activity_hour (hour_start, node_id, packets, last_sent_at)
WHERE portnum = 1 AND is_broadcast = 1;
