# Search and NAT fixes

The search API returns `count` (this page), `total_count` (all matches in the
selected time window), and `query_ms`. Counts use the same table/filter predicates
as the page query, without its cursor or limit. All Logs includes `nat_sessions`,
`nat_sessions_v2`, and `events`. CSV avoids recomputing totals for every batch.
Counting is exact, not approximate; queries remain limited to 30 seconds and 1 GB.
If a count fails, the API reports an error rather than displaying a made-up total.
Count and page are separate queries: concurrent late arrivals or retention merges
can change the dataset between them. Pagination fixes the time window, not a
database snapshot.

MikroTik source translations take priority over PPPoE/event detection. The parser
accepts optional connection state, source-MAC metadata, optional packet length,
TCP flags, UDP, and syslog envelopes. It validates IPs, ports and both repeated
translation endpoints. Diagnostic columns are persisted consistently. Extra
pre-protocol metadata and the envelope are retained in `syslog_prefix`.
Unsupported/contradictory formats, including combined DNAT, remain raw events;
unknown trailing fields are not silently discarded. Previously stored
`nat_unparsed` events remain historical events; no automatic backfill is performed.

Details now expand into a grid across the row. NAT summaries use
`subscriber | private:port → public:port → destination:port | PROTOCOL`.
Page load, browser refresh, and navigation to Search automatically search.
There is no timed refresh interrupting an investigation. Date display defaults to
Asia/Karachi; the upgrade sets existing branding to that timezone. Storage remains
UTC. Router RFC3164 timezone is a separate configuration and is not guessed from
the display timezone. Settings can still change display timezone.

System Health uses actual active-part compression totals and a short card layout.
Worker counters are a readable grid. Forecast confidence and observation duration
remain in the explanatory text. Missing history is unavailable, not fabricated.
Daily growth is net disk change normalized to a day, with its observation window
shown. Free-disk forecasts exclude backups and merge headroom.

## Safe VM upgrade

After transferring/pushing this version and updating `/opt/SysLog`, run as root:

```bash
cd /opt/SysLog
bash deploy/upgrade-console.sh
```

The script uses the existing systemd environment file without displaying its
password. It creates a consistent SQLite backup and a ClickHouse schema/size
snapshot in `data/backups`, creates any missing structured tables, adds missing
V2 diagnostic columns, validates column types, sets Asia/Karachi display time,
then restarts the listener/API and checks health. No existing row, table, TTL,
sort key, codec, index or spool is removed or rewritten. The schema snapshot is
not a full data backup. Existing diagnostic values lost by an older writer cannot
be reconstructed; historical missing fields use ClickHouse defaults.

Both schema files now share structured definitions. `schema.sql` also retains
the optional original `syslogs` definition. No automatic index or codec changes
are applied to existing tables. If validation detects incompatible types, the
upgrade stops before service restart and requires reviewing the saved schema.

No new Python dependency is required. The parser's unlisted `python-dotenv`
import was removed; services continue using their existing environment file.

## Duplicate investigation

Each received packet produces at most one row. Similar NAT tuples may represent
separate firewall packet logs; `record_type=packet_snat` does not assert that a
connection was created. The spool keeps its UUID on retry, but an ambiguous
ClickHouse response followed by retry can duplicate an insert in MergeTree.
No exactly-once guarantee or endpoint-based deduplication is claimed.

`duplicate_audit.py`, run with the service environment, reports repeated UUIDs
and repeated translation/timestamp tuples over at most 100,000 rows per table
in the last hour. It does not delete anything. Production duplicate counts have
not been observed from this workspace; do not label all similar rows duplicates.

## Validation

- 26 unit tests: parser variations, metadata, invalid fallbacks, packet identity,
  mixed numeric searches, totals, confidence/gaps, authentication, authorization,
  device lifecycle, CSV safety, request limits and durable retries.
- Real ClickHouse 26.8.2.7: 262 rows across NAT/events, exact totals across three
  pages, five structured searches, numeric mixed-kind search, diagnostic values.
  A 12-column V2 table was upgraded twice and its pre-existing row survived.
- Real Edge: initial/reload/navigation auto-load, totals, next/previous, NAT
  columns, detail grid, Karachi timestamps, health cards, auth/Devices/CSV/branding.
  Browser search/health responses were stubbed; SQL was verified separately.
- Real UDP: pending → approve → batch/spool insertion → block → graceful stop.
  The UDP test substitutes a file sink for ClickHouse.
- Python compile/import, JavaScript syntax, Bash syntax and diff whitespace checks.

Tests were local. This does not certify production VM throughput or remotely
deploy changes. The production ClickHouse 25.8 installation still needs the safe
upgrade and live traffic verification.

## Measured codec comparison from the local sample

20,000 seeded synthetic MikroTik records, 2,000 subscribers, two protocols, one
router; ClickHouse 26.8.2.7. Each table was merged before measuring storage. This is
not the production screenshot's traffic mix or its 11.44x ratio.

| Table / codec | Uncompressed bytes | Compressed bytes | Bytes/row | Ratio | Insert seconds | Server insert CPU seconds | Query median ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| Previous raw Events / ZSTD(1) | 9,690,254 | 1,594,551 | 79.73 | 6.08x | 1.338 | 0.0223 | 64.72 |
| Structured / ZSTD(1) | 2,309,545 | 588,932 | 29.45 | 3.92x | 1.515 | 0.0159 | 63.75 |
| Structured / ZSTD(3) | 2,309,545 | 548,757 | 27.44 | 4.21x | 1.422 | 0.0125 | 56.23 |
| Structured / ZSTD(6) | 2,309,545 | 545,185 | 27.26 | 4.24x | 1.354 | 0.0121 | 52.01 |
| Structured / ZSTD(9) | 2,309,545 | 543,296 | 27.16 | 4.25x | 1.496 | 0.0133 | 59.10 |

Structured ZSTD(1) reduced compressed bytes by 63.07% versus the raw event
representation despite a lower compression ratio. CPU excludes background
merges; times include client/HTTP overhead and are a single local run. The current V2 creation default is ZSTD(3), based on this comparison. Existing
columns keep their codecs unless the separate compression plan is applied; no
forced historical recompression runs. Source-NAT types and diagnostic fields
are unchanged. No annual footprint is inferred from
this synthetic run; production EPS/history are unavailable here.


## Local test incident

An initial test import loaded workspace configuration before the temporary test
database was set. That run overwrote the laptop's `devices.db` users/device state.
The production VM and ClickHouse log tables were not affected. No prior local
configuration backup was found; the overwritten state could not be recovered.
Known-password test accounts were disabled. The fixture now pins configuration
to its temporary database and checks the path before cleanup; the final suite
verified the workspace database was byte-for-byte unchanged. To use the laptop
console again, create a new local administrator with `manage.py create-admin`
and re-add local device approvals. This incident does not change VM credentials.
