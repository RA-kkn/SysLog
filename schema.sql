-- Structured definitions are mirrored from schema_structured.sql.
-- Additive columns and retry setting; existing TTL/order/codecs are unchanged.
-- Additive only. Does not change syslogs or its existing 90-day TTL.
CREATE DATABASE IF NOT EXISTS syslog_db;
CREATE TABLE IF NOT EXISTS syslog_db.nat_sessions
(
 timestamp DateTime64(3, 'UTC') CODEC(Delta, ZSTD(1)),
 received_at DateTime64(3, 'UTC') CODEC(Delta, ZSTD(1)),
 record_id UUID CODEC(ZSTD(1)),
 router_ip IPv4 CODEC(ZSTD(1)),
 private_ip IPv4 CODEC(ZSTD(1)),
 private_port UInt16 CODEC(ZSTD(1)),
 public_ip IPv4 CODEC(ZSTD(1)),
 public_port UInt16 CODEC(ZSTD(1)),
 destination_ip IPv4 CODEC(ZSTD(1)),
 destination_port UInt16 CODEC(ZSTD(1)),
 protocol LowCardinality(String) CODEC(ZSTD(1)),
 subscriber_id String CODEC(ZSTD(1))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (toDate(timestamp), router_ip, timestamp, record_id)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

CREATE TABLE IF NOT EXISTS syslog_db.events
(
 timestamp DateTime64(3, 'UTC') CODEC(Delta, ZSTD(1)),
 received_at DateTime64(3, 'UTC') CODEC(Delta, ZSTD(1)),
 record_id UUID CODEC(ZSTD(1)),
 router_ip IPv4 CODEC(ZSTD(1)),
 source_port UInt16 CODEC(ZSTD(1)),
 hostname LowCardinality(String) CODEC(ZSTD(1)),
 facility UInt8 CODEC(ZSTD(1)),
 severity UInt8 CODEC(ZSTD(1)),
 event_type LowCardinality(String) CODEC(ZSTD(1)),
 message String CODEC(ZSTD(1)),
 raw_message String CODEC(ZSTD(1))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (toDate(timestamp), router_ip, timestamp, record_id)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

-- Beside historical tables; no backfill, rename or deletion.
CREATE TABLE IF NOT EXISTS syslog_db.nat_sessions_v2
(
 timestamp DateTime64(3, 'UTC') CODEC(Delta, ZSTD(9)),
 received_at DateTime64(3, 'UTC') CODEC(Delta, ZSTD(9)),
 record_id UInt64 CODEC(Delta, ZSTD(9)),
 router_ip IPv4 CODEC(ZSTD(9)),
 private_ip IPv4 CODEC(ZSTD(9)),
 private_port UInt16 CODEC(ZSTD(9)),
 public_ip IPv4 CODEC(ZSTD(9)),
 public_port UInt16 CODEC(ZSTD(9)),
 destination_ip IPv4 CODEC(ZSTD(9)),
 destination_port UInt16 CODEC(ZSTD(9)),
 protocol LowCardinality(String) CODEC(ZSTD(9)),
 subscriber_id String CODEC(ZSTD(9)),
 source_port UInt16 CODEC(ZSTD(9)),
 input_interface String CODEC(ZSTD(9)),
 output_interface LowCardinality(String) CODEC(ZSTD(9)),
 connection_state LowCardinality(String) CODEC(ZSTD(9)),
 tcp_flags LowCardinality(String) CODEC(ZSTD(9)),
 packet_length UInt16 CODEC(ZSTD(9)),
 syslog_prefix String CODEC(ZSTD(9)),
 record_type LowCardinality(String) CODEC(ZSTD(9)),
 field_mask UInt8 DEFAULT 63 CODEC(ZSTD(9)),
 raw_message String CODEC(ZSTD(9)),
 raw_bytes_b64 String CODEC(ZSTD(9)),
 parse_status LowCardinality(String) DEFAULT 'legacy' CODEC(ZSTD(9)),
 application LowCardinality(String) CODEC(ZSTD(9)),
 migration_source LowCardinality(String) CODEC(ZSTD(9))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (toDate(timestamp), router_ip, timestamp, record_id)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;

-- Metadata-only compatibility for installations with the earlier 12-column V2.
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS source_port UInt16 CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS input_interface String CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS output_interface LowCardinality(String) CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS connection_state LowCardinality(String) CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS tcp_flags LowCardinality(String) CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS packet_length UInt16 CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS syslog_prefix String CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS record_type LowCardinality(String) CODEC(ZSTD(9));

-- Optional original history table.
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

-- Metadata-only compatibility for installations with the earlier 12-column V2.
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS source_port UInt16 CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS input_interface String CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS output_interface LowCardinality(String) CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS connection_state LowCardinality(String) CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS tcp_flags LowCardinality(String) CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS packet_length UInt16 CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS syslog_prefix String CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS record_type LowCardinality(String) CODEC(ZSTD(9));

-- NAT-only normalization: existing native fields/types/codecs/TTL unchanged.
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS field_mask UInt8 DEFAULT 63 CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS raw_message String CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS raw_bytes_b64 String CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS parse_status LowCardinality(String) DEFAULT 'legacy' CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS application LowCardinality(String) CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 ADD COLUMN IF NOT EXISTS migration_source LowCardinality(String) CODEC(ZSTD(9));
ALTER TABLE syslog_db.nat_sessions_v2 MODIFY SETTING non_replicated_deduplication_window = 10000;
