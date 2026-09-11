-- ClickHouse schema for syslog storage
-- High compression + fast search by ip / port / severity / time / keyword

CREATE DATABASE IF NOT EXISTS syslog_db;

CREATE TABLE IF NOT EXISTS syslog_db.syslogs
(
    -- jab humne receive kiya (proper detailed timestamp, ms precision)
    received_at      DateTime64(3)              CODEC(Delta, ZSTD),

    -- syslog message ke andar se nikala hua timestamp (agar mila)
    device_time       DateTime64(3)              CODEC(Delta, ZSTD),

    device_ip         IPv4                       CODEC(ZSTD),
    source_port       UInt16                     CODEC(ZSTD),

    facility          UInt8                      CODEC(ZSTD),
    severity          UInt8                      CODEC(ZSTD),

    hostname          LowCardinality(String)     CODEC(ZSTD),
    process_name      LowCardinality(String)     CODEC(ZSTD),
    pid               UInt32                     CODEC(ZSTD),

    message           String                     CODEC(ZSTD(3)),
    raw_message       String                     CODEC(ZSTD(3))
)
ENGINE = MergeTree()
PARTITION BY toDate(received_at)
ORDER BY (device_ip, received_at)
TTL toDateTime(received_at) + INTERVAL 90 DAY   -- purana data auto delete, apni requirement pe adjust karo
SETTINGS index_granularity = 8192;

-- Search ko aur fast karne ke liye extra skip indexes
ALTER TABLE syslog_db.syslogs ADD INDEX IF NOT EXISTS idx_severity severity TYPE set(10) GRANULARITY 4;
ALTER TABLE syslog_db.syslogs ADD INDEX IF NOT EXISTS idx_hostname hostname TYPE bloom_filter GRANULARITY 4;
ALTER TABLE syslog_db.syslogs ADD INDEX IF NOT EXISTS idx_message message TYPE tokenbf_v1(30720, 3, 0) GRANULARITY 4;
