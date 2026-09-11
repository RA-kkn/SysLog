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
 subscriber_id String CODEC(ZSTD(1)),
 source_port UInt16 CODEC(ZSTD(1)),
 input_interface String CODEC(ZSTD(1)),
 output_interface LowCardinality(String) CODEC(ZSTD(1)),
 connection_state LowCardinality(String) CODEC(ZSTD(1)),
 tcp_flags LowCardinality(String) CODEC(ZSTD(1)),
 packet_length UInt16 CODEC(ZSTD(1)),
 syslog_prefix String CODEC(ZSTD(1)),
 record_type LowCardinality(String) CODEC(ZSTD(1))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (toDate(timestamp), router_ip, timestamp, record_id)
TTL toDateTime(timestamp) + INTERVAL 365 DAY DELETE;
