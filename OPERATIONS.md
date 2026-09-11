# Operations and validation report

## Scope and current state

This is an in-place upgrade of the existing Python/FastAPI/SQLite/static HTML project. The source backup is `backups/source-20260911-093645.zip`; the original findings are in `AUDIT.md`. No production database, firewall, RADIUS service or running Docker daemon was available. No production data or existing TTL was modified.

Flow: UDP receiver → bounded memory queue → application device authorization → parser → separate NAT/Event batches → bounded durable SQLite spool → retrying ClickHouse writer → authenticated FastAPI search/export → console. Authorization is checked before parsing/insertion; the receive queue holds a bounded amount of unapproved traffic. Device attempts are aggregated into SQLite transactions. The listener cache refreshes within five seconds; the UI compares heartbeat authorization fingerprints before reporting application ACL applied. This does not claim a firewall rule was installed.

## Defaults and schema

| Setting | Value |
|---|---|
| Config database | Existing `devices.db`, SQLite WAL |
| Config tables | devices, settings, users, sessions, login_limits, ingest_minutes |
| Log database | `syslog_db` |
| New log tables | `nat_sessions`, `events` |
| Legacy table | `syslogs`, preserved, separate bounded Legacy history search |
| Engine | MergeTree |
| Partition key | `toYYYYMM(timestamp)` |
| Order key | `(toDate(timestamp), router_ip, timestamp, record_id)` |
| Codecs | Delta + ZSTD(1) on timestamps; ZSTD(1) on other columns |
| TTL | New tables: `toDateTime(timestamp) + INTERVAL 365 DAY DELETE` |
| Legacy TTL | Original schema: 90 days; unchanged, requires review |
| Listener | UDP 514, IPv4; configurable |
| API | 127.0.0.1:8000 recommended behind TLS proxy |
| Batch | 2,000 rows OR 1 second |
| Receive queue | 50,000 packets per worker |
| Disk spool | 1 GiB serialized payload per worker, plus SQLite/WAL overhead |
| Workers | 1 default; SO_REUSEPORT on supported operating systems |
| Auth | scrypt password hashes; random server-side cookie sessions; hashed tokens; CSRF token |
| Sessions | 8 hours; HttpOnly, SameSite=Strict, Secure by default |
| Roles | ADMIN, OPERATOR, VIEWER; latter two read-only plus optional export |
| Export | Streaming 1,000-row query pages; maximum 50,000 rows |

The day-first sort key favors the mandatory date range and router search. It is a starting point, not a claim of optimal IP/subscriber performance at billions of rows. UUID gives cursor tie-breaking and stable identity across retries, at a storage cost. Subscriber IDs use String until actual cardinality/compression is measured; protocol/hostname/event category use LowCardinality. No unbenchmarked skip index or projection is installed. No hardware-dependent hot/warm policy is required.

`schema.sql` is retained as the original legacy definition for reference; new installs use `schema_structured.sql` or `manage.py init-schema`. `CREATE IF NOT EXISTS` does not update tables already present. Changing `RETENTION_DAYS` affects newly created tables and the target display, not an existing TTL. Generate review SQL using `manage.py retention-plan`. TTL expiry is asynchronous during merges; this is not an exact deletion-time SLA.

## NAT support and preservation

Actual vendor/model/log samples were unavailable. The conservative compact parser currently accepts this **documented test format**:

```text
NAT private_ip=100.64.0.2 private_port=12345 public_ip=203.0.113.10 public_port=54321 destination_ip=198.51.100.20 destination_port=443 protocol=tcp subscriber_id=example
```

Fields must be complete and unique, IPs must be IPv4, ports 0..65535, protocol TCP/UDP. This bare format has no device timestamp, so timestamp is receive time. Unknown/incomplete NAT, IPv6, extra fields, or vendor syslog envelopes stay in Events with original decoded text. Do not infer vendor NAT coverage from synthetic fixtures. Obtain representative router lines and extend/test a vendor mapping before relying on NAT compression. Existing RFC3164/RFC5424 generic parsing is retained. RFC3164 timezone is assumed UTC; configure routers to UTC or add a vendor timezone mapping. Invalid UTF-8 is replaced during decoding.

NAT stores no duplicate raw text only for the fully recognized format. Events retain message plus raw text to avoid losing envelope metadata. Raw retention is intentionally not a blanket destructive setting.

## Storage metrics and the 4 TB target

Current DB size, compression, production EPS, daily physical growth, yearly footprint and maximum hardware EPS are **unmeasured** here. Dashboard queries `system.parts` and `system.disks`; it never manufactures measurements when ClickHouse is unavailable. Net active-part disk growth is measured between persisted snapshots (merges and TTL can make it negative); it is shown separately from estimated compressed ingest. Run `.venv/Scripts/python.exe monitoring.py --watch` as a separate supervised process for regular minute samples without opening the dashboard.

For structured tables, it computes average compressed/uncompressed bytes per active row. Acknowledged insert totals are retained by minute in SQLite; observed EPS uses up to the last 24 hours, with a minimum one-minute observation. Projected rows/day = EPS × 86,400; estimated raw/compressed bytes/day = rows/day × corresponding bytes/row; multiply by 30 or 365 for projections. These are ingest projections, not measured filesystem growth. First-minute partial coverage and changing traffic/table mix can bias short samples. Compare a full representative day, including spool drain and merge completion. Statistics can undercount if their separate SQLite update fails; this is logged. Replay after an ambiguous acknowledgment can duplicate records.

10 decimal GB/day × 365 = 3.65 decimal TB/year, before operational reserve. A 4 TB budget allows about 10.96 GB/day of retained data. At measured compressed `b` bytes/row, theoretical EPS budget is `4e12 / (365 * 86400 * b)`. Do not confuse this capacity calculation with tested maximum ingest throughput. Leave space for merges, indexes, spool, filesystem overhead, backups and replicas. Dashboard free-disk totals across multiple ClickHouse disks may overcount shared underlying filesystems; inspect individual disks before capacity decisions.

## Local startup (PowerShell, from this project directory)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Set a real password in each service environment; do not commit it.
$env:CLICKHOUSE_PASSWORD = Read-Host 'ClickHouse password'
docker compose up -d
.\.venv\Scripts\python.exe manage.py init-schema
.\.venv\Scripts\python.exe manage.py create-admin admin
# Only for loopback HTTP development; production must use HTTPS and Secure cookies.
$env:COOKIE_SECURE = 'false'
.\.venv\Scripts\python.exe -m uvicorn search_api:app --host 127.0.0.1 --port 8000
```

In a second terminal, set the same ClickHouse environment and run:

```powershell
.\.venv\Scripts\python.exe listener.py
```

Open `http://127.0.0.1:8000`. Sign in → Devices → enter router IPv4 → Approve IP. Alternatively let routers send first, then Allow their Pending entries. Only new approved packets are retained; pending packets are denied, counted, and are not recoverable as history. Blocking stops future ingest after the cache refresh; existing history remains searchable. Use Router IP in Log Search to show one router.

Python reads environment variables, not `.env` automatically. Docker Compose can read `.env`; ensure API/listener receive the same values. Existing installations may use different ClickHouse credentials; retain those rather than assuming the new fresh-install user exists. Do not switch production ClickHouse versions blindly. The Compose 25.8 family tag is a baseline, not an assertion of current security support; pin a tested image digest for your deployment.

## Production TLS and services

Run API behind a TLS reverse proxy with a hostname and a valid certificate. An example Caddy config is in `deploy/Caddyfile.example`. Replace its hostname with your actual DNS name. Internal CA certificates must be installed into client trust stores; do not suppress browser warnings. Keep COOKIE_SECURE=true. Bind ClickHouse to loopback or a protected management network; Compose now publishes database ports only on 127.0.0.1. API proxy headers must be trusted only from the proxy IP.

Linux startup commands with a provisioned virtual environment and protected environment file:

```bash
set -a
. /etc/syslog-console.env
set +a
.venv/bin/python manage.py init-schema
.venv/bin/python manage.py create-admin admin
.venv/bin/python -m uvicorn search_api:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
# Separate service (grant CAP_NET_BIND_SERVICE through systemd for UDP 514):
.venv/bin/python listener.py
```

Use a service supervisor with restart-on-failure and enough shutdown time for queue drain; restrict access to config, spool and backups. Do not use `--reload` in production. Set proxy request-body limit to 3 MiB, request rate limits, and log rotation. API enforces the body limit independently. Maintain OS UDP counters (`netstat -su` on Linux), alert on receive errors, app queue/spool drops, stale heartbeat, write failures and low disk. Firewall/RADIUS rules remain your existing infrastructure responsibility; no integration was present to preserve.

## Backup commands

The source ZIP made during this upgrade is not a production database backup. Take a consistent SQLite config backup at any time:

```powershell
.\.venv\Scripts\python.exe manage.py backup-config backups/devices-backup.db
```

For a consistent cold full backup, first stop the listener and API gracefully (Ctrl+C in their terminals, or stop their services). Then:

```powershell
docker compose stop clickhouse
New-Item -ItemType Directory -Force backups | Out-Null
docker compose run --rm --no-deps --user root --volume "${PWD}/backups:/backup" --entrypoint tar clickhouse -czf /backup/clickhouse-cold.tar.gz -C /var/lib/clickhouse .
Compress-Archive -Path devices.db,data,static,config.py,device_store.py,auth.py,database.py,listener.py,parser.py,search.py,search_api.py,monitoring.py,manage.py,schema_structured.sql,docker-compose.yml,requirements.txt -DestinationPath backups/application-cold.zip
docker compose start clickhouse
```

Choose new filenames for subsequent snapshots. Copy backups off-host and protect them as sensitive data. Back up service environment/secrets separately with restricted permissions. Check available disk space first. These Docker backup commands could not be exercised without a running daemon.

## Restore procedure

1. Use a replacement host/empty recovery directory, the same tested ClickHouse image/version and Docker Compose project configuration. Keep production intact until recovery is verified. Restore environment/secrets separately.
2. Extract application-cold.zip into the recovery directory. It includes devices.db, the complete stopped spool directory, and branding uploads. Recreate the venv and install requirements. Existing queued batches replay with stable UUIDs.
3. Ensure the recovery ClickHouse volume is **empty**, with no server process using it. Inspect before extracting:

```powershell
docker compose run --rm --no-deps --entrypoint ls clickhouse -la /var/lib/clickhouse
```

4. Only after confirming the destination volume is empty, restore:

```powershell
docker compose run --rm --no-deps --user root --volume "${PWD}/backups:/backup:ro" --entrypoint tar clickhouse --keep-old-files -xzf /backup/clickhouse-cold.tar.gz -C /var/lib/clickhouse
docker compose up -d clickhouse
.\.venv\Scripts\python.exe manage.py storage
```

5. Before restarting API/listener, revoke restored session tokens:

```powershell
.\.venv\Scripts\python.exe -c "import device_store; c=device_store.sqlite3.connect(device_store.DB_PATH); c.execute('DELETE FROM sessions'); c.commit(); c.close()"
```

6. Start API/listener with the startup commands above. Verify users, device approvals, table counts, oldest/newest timestamps, recent searches and spool drain before cutover. Perform an actual restore drill; the procedure is documented but not verified against production here.

For config-only recovery, stop API/listener, preserve the current database and its WAL/SHM files as a separate backup, then use SQLite's backup API to restore `devices-backup.db` into the target config DB; do not overwrite only the main file while an old WAL is active. Full cold directory restore is preferred.

## Tests and controlled benchmarks

```powershell
.\.venv\Scripts\python.exe -m pip install httpx
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check static/console.js
.\.venv\Scripts\python.exe manage.py storage
.\.venv\Scripts\python.exe manage.py retention-plan --days 365
```

Use an isolated listener/ClickHouse database and approve the generator source. Never run these against production without a planned load-test window:

```powershell
.\.venv\Scripts\python.exe benchmark.py --host 127.0.0.1 --port 5514 --eps 1000 --seconds 10 --confirm-test-target
.\.venv\Scripts\python.exe benchmark.py --host 127.0.0.1 --port 5514 --eps 5000 --seconds 10 --confirm-test-target
.\.venv\Scripts\python.exe benchmark.py --host 127.0.0.1 --port 5514 --eps 10000 --seconds 10 --confirm-test-target
.\.venv\Scripts\python.exe benchmark_codecs.py --rows 100000 --confirm-test-target
```

Capture system reports before/during/after; compare received, denied, parsed, spooled, inserted, failed/dropped, queue size, insert latency, CPU/RAM, and disk bytes. Codec test creates uniquely named benchmark databases and leaves tables for inspection. It compares LZ4, ZSTD(1), ZSTD(3) on synthetic NAT; generation cost and ongoing merges affect results. Repeat with representative router data and query patterns before choosing a production codec/index/order key. No such load/codec test ran automatically.

## Remaining production gates

- Real ClickHouse schema execution, timestamp binding, query results, restart/reconnect and end-to-end UI/export against the actual database must be verified.
- Real NAT vendor mappings, timezone rules and representative compressed storage must be measured before claiming the 4 TB target.
- UDP has no delivery guarantee. Memory-queued/not-yet-spooled records can be lost on abrupt process/power failure. Bounded overflow is counted; kernel losses require OS counters. For stronger guarantees deploy a durable TCP/RELP-capable edge collector.
- Spool payload is bounded; SQLite file/WAL overhead and high-water allocation require additional space. An insert that succeeds but loses its acknowledgment can be replayed as duplicates (at-least-once behavior, not exactly-once). A poison batch blocks that worker's spool and emits repeated failures; inspect it instead of deleting it silently.
- Authentication is local with no MFA; deploy behind protected admin access. All permitted viewers can search all stored routers. IP authorization does not authenticate UDP source addresses cryptographically.
- Legacy history is a separate bounded search because old rows lack a unique cursor key; no unsafe bulk migration was run. Legacy 90-day retention remains a rollout concern.
- Frontend runtime environment settings are read-only; retention modifications are reviewed CLI SQL, not automatic destructive UI actions. Active-part disk-growth snapshots require the monitor process or dashboard polling; production alert delivery still needs operational integration.

## Files modified / added

Modified existing files: `config.py`, `device_store.py`, `listener.py`, `parser.py`, `search_api.py`, `docker-compose.yml`, `requirements.txt`, `README.md`, `.gitignore`, `static/index.html`.

Added implementation: `auth.py`, `database.py`, `search.py`, `monitoring.py`, `schema_structured.sql`, `static/console.css`, `static/console.js`.

Added operations/tests: `AUDIT.md`, `OPERATIONS.md`, `.env.example`, `manage.py`, `benchmark.py`, `benchmark_codecs.py`, `deploy/Caddyfile.example`, `requirements-dev.txt`, `tests/test_platform.py`, `tests/browser_smoke.py`, `tests/udp_smoke.py`. Source snapshot is under `backups`; browser screenshots under `test-artifacts`.

## Executed validation

- 21 Python tests passed: parser/fallback, spool persistence/bounds/retry, auth, roles, CSRF, expiry, last-admin protection, manual device lifecycle, input/upload/body-limit validation, cross-origin rejection, query construction/cursor, and CSV safety. ClickHouse calls were test doubles.
- Node JavaScript syntax check passed.
- Real Edge browser smoke passed: login, manual approval, rename, block, escaped log text, CSV download, branding, tablet rendering, and viewer restrictions. Search data was synthetic; configuration/auth/API were real and isolated. Screenshots are `test-artifacts/login.png`, `devices.png`, `settings-tablet.png`.
- Three-packet UDP smoke passed: received 3, parsed/inserted 1, denied 2, queue/spool drops 0, write failures 0. It exercised the real socket, Pending/Approve/Block, parser, durable batch and graceful shutdown with an isolated file sink; no live ClickHouse claim.
- No 1k/5k/10k EPS load test, codec benchmark, production TLS, database restore, or live ClickHouse end-to-end test was executed. Docker daemon was unavailable.

References used: [ClickHouse Python driver/session guidance](https://github.com/ClickHouse/clickhouse-docs/blob/main/docs/integrations/language-clients/python/driver-api.md), [MergeTree TTL behavior](https://github.com/ClickHouse/ClickHouse/blob/master/docs/en/engines/table-engines/mergetree-family/mergetree.md). Live metrics and scalability are not inferred from documentation.
