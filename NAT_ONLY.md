# NAT-only upgrade

The listener now writes **only `nat_sessions_v2`**. Every authorized packet that reaches
the parser produces one row, including unknown, incomplete, empty and invalid-UTF8
packets. Device approval still applies: denied sources are counted, not ingested.
Historical Events/legacy tables remain on disk and are visible in storage monitoring,
but are no longer selectable in search/export. No source data is deleted by this change.

## Parser and storage

1. Read the syslog timestamp; fall back to `received_at` if absent/invalid.
2. Try the existing MikroTik SNAT parser (including `, prio 7->0, len 52`) and strict
   key/value NAT format. NAT retains priority over PPPoE identification.
3. Otherwise extract validated labelled endpoints or an explicit `NAT (a->b)->c`
   expression, protocol/application and PPPoE/subscriber labels. Unlabelled arbitrary
   addresses are not assigned invented NAT roles. Conflicting labelled fields become unknown.
4. Store remaining fields as defaults, with presence bits distinguishing unknown values
   from genuine zero values. Unexpected parser exceptions also preserve a fallback row
   and increment the parser-error counter.
5. Preserve full original UTF8 text, including whitespace, for valid and fallback rows.
   Invalid UTF8 also retains base64 of the exact packet bytes. The raw columns never
   appear in the search response or CSV. Existing rows without raw cannot be reconstructed.

Example: `NAT private_ip=10.0.0.1 private_port=0 proto ICMP in:<pppoe-test>` becomes
private IP `10.0.0.1`, private port `0`, protocol `ICMP`, subscriber `pppoe-test`;
public/destination fields display blank. `router rebooted` still becomes one row,
with its timestamp and blank unavailable fields. IPv6/unsupported formats retain raw
for future reparsing; this upgrade preserves the existing native IPv4 schema.

Six additive columns are mirrored in both schema files:

| Column | Type/default | Purpose |
|---|---|---|
| field_mask | UInt8 DEFAULT 63 | Six endpoint presence bits; historical NAT stays complete |
| raw_message | String | Full original text, hidden |
| raw_bytes_b64 | String | Exact bytes when UTF8 is invalid, hidden |
| parse_status | LowCardinality(String) DEFAULT 'legacy' | parsed/partial/unknown/error diagnostic |
| application | LowCardinality(String) | Explicit application value |
| migration_source | LowCardinality(String) | Historical migration provenance |

Presence bits: private IP=1, private port=2, public IP=4, public port=8,
destination IP=16, destination port=32. Unset native IP/port defaults (`0.0.0.0`/`0`)
are returned as null and displayed/exported blank; known zero values remain searchable.
New columns use ZSTD(9). Existing types, codecs, ordering and TTL are preserved.
The compatibility ADD statements also prepare older 12-column V2 installations.
`non_replicated_deduplication_window=10000` enables retry tokens on MergeTree.
Schema setup retains IF NOT EXISTS definitions for historical tables for compatibility;
the production writer never targets them.

Full raw preservation increases storage compared with the previous raw-free NAT design.
Health reports measured active-part bytes and ratio; older compression benchmarks do
not describe this new format. No forced recompression or fabricated ratio is applied.

## Delivery limits and retries

UDP -> bounded queue -> normalizer -> durable bounded spool -> batch writer remains.
Normal insertion has no extra ClickHouse lookups. Retries/recovered spool batches use
batch-level UUID lookups and stable insert tokens. Each separate received packet gets
its own UUID, so identical-looking legitimate packets are not deduplicated.
Old Events/NAT spool batches drain to V2 with their original UUIDs.

This is not an unconditional exactly-once transport guarantee. UDP can lose packets
before reception; queue/spool exhaustion, a hard crash before durable spooling, and
overload still matter. These tests do not certify 100,000 packets/sec. Check kernel
UDP counters separately (`nstat -az UdpInErrors UdpRcvbufErrors`) and compare sender
counts with received counts. Application queue/spool/processing drops are explicit.
ClickHouse tokens have a finite deduplication window; recovery lookups help with older
committed batches, but overlapping in-flight retries beyond that window are not an
unbounded exactly-once guarantee. See [ClickHouse retry documentation](https://github.com/ClickHouse/clickhouse-docs/blob/main/docs/guides/developer/deduplicating-inserts-on-retries.md).

## Native VM deployment

Changes are local source changes until pushed/deployed. After publishing this revision
to your repository, run on the existing VM as root:

```bash
cd /opt/SysLog
git pull --ff-only
bash deploy/upgrade-console.sh
```

The upgrade saves configuration/schema snapshots, applies additive schema, validates
columns, and restarts the existing listener/API. It reads the existing protected
EnvironmentFile without displaying or modifying credentials. A schema snapshot is
not a backup of log rows. Keep your normal ClickHouse data backups. Existing TTL
expiry continues; migration does not suspend it. No package upgrade is required.

For the administrative commands below, define this helper in the root VM shell:

```bash
run_admin() {
  systemd-run --quiet --wait --pipe --collect \
    --property=User=syslog-console --property=Group=syslog-console \
    --property=EnvironmentFile=/etc/syslog-console.env \
    --working-directory=/opt/SysLog \
    /opt/SysLog/.venv/bin/python "$@"
}
```

## Historical migration (explicit, never automatic)

First complete the upgrade and let old spool batches drain. Choose a fixed historical
UTC window, with no old-version writer or other migration writing Events. Keep windows
small enough to inspect and within the existing retention period. Run one migration
process on this VM; do not run concurrent copies from other hosts. The CLI holds an
exclusive lock, retains source UUIDs, compares all destination fields, verifies source
and target counts and fails on duplicate UUIDs/conflicts. Distinct source UUIDs with
identical endpoints remain distinct. Generic historical Events remain unchanged.

Example window (adjust dates to your actual retained data):

```bash
run_admin migrate_events.py --start 2026-09-11T00:00:00Z --end 2026-09-12T00:00:00Z --report data/events-review.json
run_admin migrate_events.py --start 2026-09-11T00:00:00Z --end 2026-09-12T00:00:00Z --apply --report data/events-applied.json
run_admin migrate_events.py --start 2026-09-11T00:00:00Z --end 2026-09-12T00:00:00Z --report data/events-verified.json
```

Reports require new filenames, preventing accidental overwrite. An interrupted apply
can be rerun: previously inserted matching UUIDs are verified, not inserted again.
If a hard kill leaves `data/events-migration.lock`, confirm that process has exited
before manually removing that lock file. Never remove database data to resolve a lock.
If destination contents differ (including an earlier spool migration), investigate
instead of overwriting them. If the source changes/expires mid-run, counts fail verification.

Successful final report: `source_rows == scanned`, `source_unchanged: true`,
`eligible == verified`, `missing: 0`, `mismatched: 0`, `complete: true`.
`retained_only` accounts for non-eligible Events; it must not be mistaken for lost rows.
A second apply reports `inserted: 0`. Source Events are never deleted, even after success.
Deletion/drop remains a separate manual decision; no destructive SQL is supplied here.

## Verify received vs stored

Use a controlled test with all senders paused before the baseline and after the test;
allow queues, parser batches and spool to drain, then wait for fresh heartbeats. Do not
restart workers or run historical migration during this test. Existing old spool must
already be empty. Use packets whose event timestamps are within retention.

```bash
run_admin verify_ingest.py begin data/nat-baseline.json
# Send a known test burst from approved routers, then pause senders and drain.
run_admin verify_ingest.py check data/nat-baseline.json
```

Expected: `received - denied == inserted == stored_rows == unique_packet_ids`;
all three drop deltas zero, `passed: true`. With only approved senders this reduces
to received=stored. Counts refer to the current test interval, not historical totals.
The verification query scans receive-time data and is intended for controlled checks,
not frequent high-volume monitoring. Recovery acknowledgements are not a lifetime DB
row ledger; use the controlled test and direct database verification together.

On the website: sign in, confirm auto-load, only NAT selectable, exact total across
pages, and CSV with precisely the same nine fields/order and Asia/Karachi timestamps.
Unknown rows must appear with blanks, not disappear. Protocol/App uses the extracted
protocol, or application, or `PROTOCOL / application` when both are explicit.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/udp_smoke.py
.venv/bin/python tests/browser_smoke.py
# Only against an isolated ClickHouse test server, with its environment configured:
.venv/bin/python tests/clickhouse_smoke.py --confirm-test-target
```

The browser test requires Playwright and Edge. UDP uses a file insert sink; real
ClickHouse integration creates a unique test database and retains it without cleanup
SQL. Production credentials/data are not used by local test fixtures.

Validated locally: 36 unit tests; Python compilation and JavaScript syntax; Edge UI
smoke; real UDP authorization/normalization/spooling test (5 received, 2 denied,
3 stored, zero application drops); real ClickHouse 26.8 isolated integration
(262 new packets/rows, retries insert no duplicates, 263 total including the
pre-upgrade fixture, both schemas applied safely, three search pages). Migration
fixture: 3 source Events, 2 eligible, 2 verified, second apply inserts zero, all
3 Events retained. These are functional tests, not a production VM/load benchmark;
the VM's ClickHouse 25.8 must still pass deployment validation.

## Exact changed-file manifest

| Area | Files |
|---|---|
| Ingestion | `parser.py`, `listener.py`, new `nat_writer.py` |
| Search/export | `search.py`, `search_api.py`, new `nat_view.py` |
| UI | `static/index.html`, `static/console.js`, `static/console.css` |
| Schema/health | `schema.sql`, `schema_structured.sql`, `schema_audit.py`, `compression.py`, `monitoring.py` |
| Administration | new `migrate_events.py`, new `verify_ingest.py`, `deploy/upgrade-console.sh` |
| Tests | `tests/test_platform.py`, `tests/test_nat_search.py`, `tests/test_compression.py`, new `tests/test_normalizer.py`, `tests/clickhouse_smoke.py`, `tests/udp_smoke.py`, `tests/browser_smoke.py` |
| Documentation | `README.md`, new `NAT_ONLY.md` |

No runtime dependency or requirements change is necessary.
