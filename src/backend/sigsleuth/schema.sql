-- Saved facts: what gateways reported, grouped into packets, and what signal
-- survey phones uploaded. Derived tables (derived.sql) are rebuilt from these.
-- FACTS_VERSION in db.py is this file's version.
-- Times are epoch milliseconds (UTC). Node numbers and packet IDs are unsigned
-- 32-bit. A node is its node number alone: topic roots are gateway settings,
-- so one radio heard through several roots is one node.

-- One radio packet, grouping its receptions (ingest.group_reception).
-- channel is the first report's. Application data (portnum, unreadable_reason,
-- application_payload, request_id, reply_id, is_reaction, is_signed) comes
-- from the first report; a later readable one replaces an unreadable result.
-- IDs follow first_received_at because ingest keeps receipt order, so time
-- windows and retention find their boundary by ID instead of a time index.
CREATE TABLE packet (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL CHECK (length(CAST(channel AS BLOB)) BETWEEN 1 AND 1024),
    sender_node_id INTEGER NOT NULL CHECK (sender_node_id BETWEEN 1 AND 4294967295),
    recipient_node_id INTEGER NOT NULL CHECK (recipient_node_id BETWEEN 0 AND 4294967295),
    packet_id INTEGER NOT NULL CHECK (packet_id BETWEEN 1 AND 4294967295),
    portnum INTEGER CHECK (portnum IS NULL OR portnum BETWEEN 0 AND 4294967295),
    has_decoded_report INTEGER NOT NULL CHECK (has_decoded_report IN (0, 1)),
    has_encrypted_report INTEGER NOT NULL CHECK (has_encrypted_report IN (0, 1)),
    encrypted_channel_hash INTEGER CHECK (encrypted_channel_hash IS NULL OR encrypted_channel_hash BETWEEN 0 AND 4294967295),
    first_received_at INTEGER NOT NULL,
    last_received_at INTEGER NOT NULL CHECK (last_received_at >= first_received_at),
    reception_count INTEGER NOT NULL CHECK (reception_count >= 0),
    unreadable_reason TEXT
        CHECK (unreadable_reason IS NULL OR length(CAST(unreadable_reason AS BLOB)) BETWEEN 1 AND 64),
    application_payload BLOB CHECK (application_payload IS NULL OR length(application_payload) <= 65536),
    request_id INTEGER CHECK (request_id IS NULL OR request_id BETWEEN 1 AND 4294967295),
    reply_id INTEGER CHECK (reply_id IS NULL OR reply_id BETWEEN 1 AND 4294967295),
    is_reaction INTEGER NOT NULL DEFAULT 0 CHECK (is_reaction IN (0, 1)),
    is_signed INTEGER NOT NULL DEFAULT 0 CHECK (is_signed IN (0, 1)),
    -- The sender's OK to MQTT bit, from the first report that carried it;
    -- NULL when none did (opt_out.py).
    ok_to_mqtt INTEGER CHECK (ok_to_mqtt IN (0, 1)),
    CHECK (has_decoded_report = 1 OR has_encrypted_report = 1)
) STRICT;

CREATE INDEX packet_identity_time
ON packet (sender_node_id, packet_id, last_received_at);

CREATE INDEX packet_sender ON packet (sender_node_id);
-- Broadcasts, most of the table, are left out to keep the index small.
CREATE INDEX packet_direct_recipient ON packet (recipient_node_id)
WHERE recipient_node_id != 4294967295;

-- Chat's text messages (port 1), a few percent of packets.
CREATE INDEX text_message ON packet (id) WHERE portnum = 1;
CREATE INDEX text_message_channel ON packet (channel, id) WHERE portnum = 1;
CREATE INDEX text_message_packet ON packet (packet_id) WHERE portnum = 1;

-- An expression index, so only queries writing coalesce(portnum, -1) use it:
-- a plain portnum index took over the node, chat and text reach queries.
CREATE INDEX packet_port ON packet (coalesce(portnum, -1));

-- One gateway's report of a packet. A repeated MQTT delivery only adds to
-- copies and keeps the first copy's time and radio facts. hop_start =
-- hop_limit alone does not prove the gateway heard the sender directly; use
-- derive.is_direct_reception. relay_byte is only the relay's last byte.
-- route_payload is the traceroute route this gateway reported; relays rewrite
-- it on the way, so each gateway's differs.
CREATE TABLE reception (
    id INTEGER PRIMARY KEY,
    packet_row_id INTEGER NOT NULL REFERENCES packet(id) ON DELETE CASCADE,
    gateway_node_id INTEGER NOT NULL CHECK (gateway_node_id BETWEEN 0 AND 4294967295),
    received_at INTEGER NOT NULL,
    copies INTEGER NOT NULL DEFAULT 1 CHECK (copies >= 1),
    rx_snr REAL,
    rx_rssi INTEGER,
    hop_start INTEGER,
    hop_limit INTEGER,
    relay_byte INTEGER,
    route_payload BLOB CHECK (route_payload IS NULL OR length(route_payload) <= 1024),
    UNIQUE (packet_row_id, gateway_node_id)
) STRICT;

-- One signal survey session: a phone and its own node (the survey node,
-- receiver_node_id) uploading every packet that node heard (coverage.py).
CREATE TABLE coverage_session (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE CHECK (length(CAST(uuid AS BLOB)) BETWEEN 1 AND 64),
    receiver_node_id INTEGER NOT NULL CHECK (receiver_node_id BETWEEN 1 AND 4294967295),
    receiver_name TEXT CHECK (receiver_name IS NULL OR length(CAST(receiver_name AS BLOB)) <= 1024),
    modem_preset TEXT CHECK (modem_preset IS NULL OR length(CAST(modem_preset AS BLOB)) <= 64),
    spreading_factor INTEGER CHECK (spreading_factor IS NULL OR spreading_factor BETWEEN 5 AND 12),
    bandwidth_khz REAL,
    frequency_mhz REAL,
    app_version TEXT CHECK (app_version IS NULL OR length(CAST(app_version AS BLOB)) <= 64),
    first_uploaded_at INTEGER NOT NULL,
    last_uploaded_at INTEGER NOT NULL CHECK (last_uploaded_at >= first_uploaded_at)
) STRICT;

-- One packet the survey node heard, with where the phone was. Not a reception.
-- A 0 SNR or RSSI is a real reading here (the phone sends NULL when missing).
-- cell_x/cell_y, heard_node_id, heard_how, candidates and margin_db are
-- decided once, at upload: the MQTT reports, links and positions they read
-- age out, so a changed rule applies only to new or re-sent uploads.
-- Uploads arrive late, so IDs do not follow time: retention uses the heard_at
-- index.
CREATE TABLE coverage_measurement (
    id INTEGER PRIMARY KEY,
    session_row_id INTEGER NOT NULL REFERENCES coverage_session(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    heard_at INTEGER NOT NULL,
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    accuracy_m REAL,
    fix_age_ms INTEGER,
    sender_node_id INTEGER NOT NULL CHECK (sender_node_id BETWEEN 1 AND 4294967295),
    recipient_node_id INTEGER CHECK (recipient_node_id IS NULL OR recipient_node_id BETWEEN 0 AND 4294967295),
    packet_id INTEGER CHECK (packet_id IS NULL OR packet_id BETWEEN 1 AND 4294967295),
    rx_snr REAL,
    rx_rssi INTEGER,
    hop_start INTEGER,
    hop_limit INTEGER,
    relay_byte INTEGER CHECK (relay_byte IS NULL OR relay_byte BETWEEN 0 AND 255),
    -- The sender's OK to MQTT bit and whether the packet's channel uses the
    -- default key. NULL when the survey node could not decode the packet or
    -- the app predates them.
    ok_to_mqtt INTEGER CHECK (ok_to_mqtt IN (0, 1)),
    default_key INTEGER CHECK (default_key IN (0, 1)),
    cell_x INTEGER NOT NULL,
    cell_y INTEGER NOT NULL,
    heard_node_id INTEGER,
    heard_how TEXT NOT NULL CHECK (heard_how IN ('direct', 'reported', 'gateway_link',
        'sender_link', 'nearest', 'only_candidate', 'unknown')),
    candidates INTEGER NOT NULL CHECK (candidates >= 0),
    margin_db REAL,
    UNIQUE (session_row_id, seq)
) STRICT;

CREATE INDEX coverage_measurement_time ON coverage_measurement (heard_at);
CREATE INDEX coverage_measurement_cell ON coverage_measurement (cell_x, cell_y, heard_at);
CREATE INDEX coverage_measurement_heard ON coverage_measurement (heard_node_id, heard_at);
CREATE INDEX coverage_measurement_sender ON coverage_measurement (sender_node_id, heard_at);

-- Each node's latest map report (decode.MapReport), the node's own MQTT
-- self-description on <root>/2/map/; neither a packet nor a reception.
-- channel is its primary channel, not the channel its packets are heard on.
-- The position is as sent, valid or not; derive.py checks it.
CREATE TABLE map_report (
    node_id INTEGER PRIMARY KEY CHECK (node_id BETWEEN 1 AND 4294967295),
    received_at INTEGER NOT NULL,
    channel TEXT CHECK (channel IS NULL OR length(CAST(channel AS BLOB)) <= 1024),
    long_name TEXT CHECK (long_name IS NULL OR length(CAST(long_name AS BLOB)) <= 1024),
    short_name TEXT CHECK (short_name IS NULL OR length(CAST(short_name AS BLOB)) <= 1024),
    role TEXT NOT NULL CHECK (length(role) <= 64),
    hardware_model TEXT CHECK (hardware_model IS NULL OR length(hardware_model) <= 64),
    firmware_version TEXT CHECK (firmware_version IS NULL OR length(firmware_version) <= 1024),
    firmware_number INTEGER,
    region TEXT CHECK (region IS NULL OR length(region) <= 64),
    modem_preset TEXT NOT NULL CHECK (length(modem_preset) <= 64),
    has_default_channel INTEGER NOT NULL CHECK (has_default_channel IN (0, 1)),
    nodes_online_local INTEGER NOT NULL,
    latitude REAL,
    longitude REAL,
    altitude_meters INTEGER,
    position_precision INTEGER NOT NULL
) STRICT;
