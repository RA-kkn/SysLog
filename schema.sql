-- ============================================================
-- ISP / NOC SYSLOG + NAT STORAGE
-- ClickHouse Schema
--
-- Goals:
--   - Structured MikroTik NAT/SNAT storage
--   - High compression
--   - Fast IP / subscriber / router / time searches
--   - Packet/event timestamp kept separately from received_at
--   - 365-day retention
--   - Existing tables are NOT dropped
-- ============================================================


CREATE DATABASE IF NOT EXISTS syslog_db;


-- ============================================================
-- 1. STRUCTURED NAT TABLE V2
-- ============================================================
--
-- This is the preferred table for successfully parsed
-- MikroTik NAT/SNAT records.
--
-- timestamp:
--     Timestamp contained in the router/syslog packet.
--
-- received_at:
--     Time our syslog server actually received the UDP packet.
--
-- Keeping these separate lets us detect network/syslog delay.
--
-- Native IPv4 + UInt16 fields use far less storage than
-- repeatedly storing IP addresses/ports inside raw strings.
-- ============================================================

CREATE TABLE IF NOT EXISTS syslog_db.nat_sessions_v2
(
    timestamp DateTime64(3, 'UTC')
        CODEC(Delta, ZSTD(3)),

    received_at DateTime64(3, 'UTC')
        CODEC(Delta, ZSTD(3)),

    record_id UUID
        CODEC(ZSTD(3)),

    router_ip IPv4
        CODEC(ZSTD(3)),

    private_ip IPv4
        CODEC(ZSTD(3)),

    private_port UInt16
        CODEC(T64, ZSTD(3)),

    public_ip IPv4
        CODEC(ZSTD(3)),

    public_port UInt16
        CODEC(T64, ZSTD(3)),

    destination_ip IPv4
        CODEC(ZSTD(3)),

    destination_port UInt16
        CODEC(T64, ZSTD(3)),

    protocol LowCardinality(String)
        CODEC(ZSTD(3)),

    subscriber_id String
        CODEC(ZSTD(3))
)
ENGINE = MergeTree()

PARTITION BY toYYYYMM(timestamp)

ORDER BY
(
    router_ip,
    private_ip,
    timestamp,
    record_id
)

TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE

SETTINGS
    index_granularity = 8192;


-- ============================================================
-- NAT SEARCH INDEXES
-- ============================================================

ALTER TABLE syslog_db.nat_sessions_v2
    ADD INDEX IF NOT EXISTS idx_public_ip
    public_ip
    TYPE bloom_filter(0.01)
    GRANULARITY 4;


ALTER TABLE syslog_db.nat_sessions_v2
    ADD INDEX IF NOT EXISTS idx_destination_ip
    destination_ip
    TYPE bloom_filter(0.01)
    GRANULARITY 4;


ALTER TABLE syslog_db.nat_sessions_v2
    ADD INDEX IF NOT EXISTS idx_private_port
    private_port
    TYPE minmax
    GRANULARITY 4;


ALTER TABLE syslog_db.nat_sessions_v2
    ADD INDEX IF NOT EXISTS idx_public_port
    public_port
    TYPE minmax
    GRANULARITY 4;


ALTER TABLE syslog_db.nat_sessions_v2
    ADD INDEX IF NOT EXISTS idx_destination_port
    destination_port
    TYPE minmax
    GRANULARITY 4;


ALTER TABLE syslog_db.nat_sessions_v2
    ADD INDEX IF NOT EXISTS idx_protocol
    protocol
    TYPE set(16)
    GRANULARITY 4;


ALTER TABLE syslog_db.nat_sessions_v2
    ADD INDEX IF NOT EXISTS idx_subscriber
    subscriber_id
    TYPE bloom_filter(0.01)
    GRANULARITY 4;


-- ============================================================
-- 2. GENERIC EVENTS TABLE
-- ============================================================
--
-- Anything that is not a completely parsed NAT/SNAT record
-- stays here.
--
-- Examples:
--   login/authentication
--   PPPoE events
--   firewall messages
--   warnings/errors
--   unsupported/incomplete NAT records
--
-- raw_message is deliberately preserved here for audit/debug.
-- ============================================================

CREATE TABLE IF NOT EXISTS syslog_db.events
(
    timestamp DateTime64(3, 'UTC')
        CODEC(Delta, ZSTD(3)),

    received_at DateTime64(3, 'UTC')
        CODEC(Delta, ZSTD(3)),

    record_id UUID
        CODEC(ZSTD(3)),

    router_ip IPv4
        CODEC(ZSTD(3)),

    source_port UInt16
        CODEC(T64, ZSTD(3)),

    hostname LowCardinality(String)
        CODEC(ZSTD(3)),

    facility UInt8
        CODEC(T64, ZSTD(3)),

    severity UInt8
        CODEC(T64, ZSTD(3)),

    event_type LowCardinality(String)
        CODEC(ZSTD(3)),

    message String
        CODEC(ZSTD(3)),

    raw_message String
        CODEC(ZSTD(3))
)
ENGINE = MergeTree()

PARTITION BY toYYYYMM(timestamp)

ORDER BY
(
    router_ip,
    timestamp,
    record_id
)

TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE

SETTINGS
    index_granularity = 8192;


-- ============================================================
-- EVENT SEARCH INDEXES
-- ============================================================

ALTER TABLE syslog_db.events
    ADD INDEX IF NOT EXISTS idx_event_type
    event_type
    TYPE set(100)
    GRANULARITY 4;


ALTER TABLE syslog_db.events
    ADD INDEX IF NOT EXISTS idx_severity
    severity
    TYPE set(10)
    GRANULARITY 4;


ALTER TABLE syslog_db.events
    ADD INDEX IF NOT EXISTS idx_hostname
    hostname
    TYPE bloom_filter(0.01)
    GRANULARITY 4;


ALTER TABLE syslog_db.events
    ADD INDEX IF NOT EXISTS idx_message
    message
    TYPE tokenbf_v1(30720, 3, 0)
    GRANULARITY 4;


-- ============================================================
-- 3. LEGACY NAT TABLE
-- ============================================================
--
-- Keep this because listener.py still supports "nat_sessions".
--
-- DO NOT DROP this table automatically.
--
-- nat_sessions_v2 is preferred for new MikroTik structured NAT.
-- ============================================================

CREATE TABLE IF NOT EXISTS syslog_db.nat_sessions
(
    timestamp DateTime64(3, 'UTC')
        CODEC(Delta, ZSTD(3)),

    received_at DateTime64(3, 'UTC')
        CODEC(Delta, ZSTD(3)),

    record_id UUID
        CODEC(ZSTD(3)),

    router_ip IPv4
        CODEC(ZSTD(3)),

    private_ip IPv4
        CODEC(ZSTD(3)),

    private_port UInt16
        CODEC(T64, ZSTD(3)),

    public_ip IPv4
        CODEC(ZSTD(3)),

    public_port UInt16
        CODEC(T64, ZSTD(3)),

    destination_ip IPv4
        CODEC(ZSTD(3)),

    destination_port UInt16
        CODEC(T64, ZSTD(3)),

    protocol LowCardinality(String)
        CODEC(ZSTD(3)),

    subscriber_id String
        CODEC(ZSTD(3))
)
ENGINE = MergeTree()

PARTITION BY toYYYYMM(timestamp)

ORDER BY
(
    router_ip,
    private_ip,
    timestamp,
    record_id
)

TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE

SETTINGS
    index_granularity = 8192;


-- ============================================================
-- LEGACY ORIGINAL SYSLOG TABLE
-- ============================================================
--
-- Preserve compatibility with the original project.
-- Do NOT DROP old syslogs data.
--
-- New application code should primarily use:
--
--     nat_sessions_v2
--     events
--
-- ============================================================

CREATE TABLE IF NOT EXISTS syslog_db.syslogs
(
    received_at DateTime64(3, 'UTC')
        CODEC(Delta, ZSTD(3)),

    device_time DateTime64(3, 'UTC')
        CODEC(Delta, ZSTD(3)),

    device_ip IPv4
        CODEC(ZSTD(3)),

    source_port UInt16
        CODEC(T64, ZSTD(3)),

    facility UInt8
        CODEC(T64, ZSTD(3)),

    severity UInt8
        CODEC(T64, ZSTD(3)),

    hostname LowCardinality(String)
        CODEC(ZSTD(3)),

    process_name LowCardinality(String)
        CODEC(ZSTD(3)),

    pid UInt32
        CODEC(T64, ZSTD(3)),

    message String
        CODEC(ZSTD(3)),

    raw_message String
        CODEC(ZSTD(3))
)
ENGINE = MergeTree()

PARTITION BY toYYYYMM(received_at)

ORDER BY
(
    device_ip,
    received_at
)

TTL toDateTime(received_at) + INTERVAL 365 DAY DELETE

SETTINGS
    index_granularity = 8192;


ALTER TABLE syslog_db.syslogs
    ADD INDEX IF NOT EXISTS idx_syslogs_severity
    severity
    TYPE set(10)
    GRANULARITY 4;


ALTER TABLE syslog_db.syslogs
    ADD INDEX IF NOT EXISTS idx_syslogs_hostname
    hostname
    TYPE bloom_filter(0.01)
    GRANULARITY 4;


ALTER TABLE syslog_db.syslogs
    ADD INDEX IF NOT EXISTS idx_syslogs_message
    message
    TYPE tokenbf_v1(30720, 3, 0)
    GRANULARITY 4;