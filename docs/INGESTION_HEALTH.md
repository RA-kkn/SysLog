# Ingestion Health monitoring

The main System Health page has one Ingestion Health card. View Details opens receiver/kernel/ring statistics, one compact worker table (one row per configured worker, including unavailable workers), services and existing ClickHouse/storage measurements. Log Search and CSV retain the existing ten-column contract.

## Accounting

All summary counters refer to the receiver's run_id. Worker snapshots from other runs are ignored. Missing/stale current-run workers cause DEGRADED; their last available current-run counters remain evidence, rather than being misclassified as loss. A stopped or stale receiver is DOWN.

Confirmed loss = current-run kernel RcvbufErrors + receiver dropped_queue + receiver dropped_transport + sum(current-run worker dropped_processing + dropped_spool).

Loss % = 100 * confirmed loss / (receiver received + current-run kernel RcvbufErrors); zero when the denominator is zero.

received minus stored is NEVER used as loss. Authorization denials, fallback parsing, parse failures that recover, retries and pending work are not counted as loss. Stored Records is the sum of current-run acknowledged writes; it can include recovery of durable batches from an earlier run and is not the all-time table row count. The latter remains in storage details.

The kernel baseline is captured once at listener startup, before socket reception, and snapshots contain kernel_baseline and kernel_run_delta. Historical kernel drops are excluded. Linux counters apply to the host/network namespace, so other UDP traffic can contribute; they cannot establish loss on port 514 alone. Unsupported/missing/reset counters are unavailable rather than guessed. Counts are confirmed observations, not a promise to detect network loss before the host or packets lost during an abrupt process failure.

Priority: DOWN for unavailable receiver; otherwise LOSS DETECTED for confirmed drops; otherwise DEGRADED for missing/stale workers, queue/ring usage >=70%, pending spool batches, write/worker failures or unavailable ClickHouse; otherwise HEALTHY. Confirmed drops remain visible for the current run rather than disappearing on the next refresh.

Queue usage is the greater of packet capacity usage and shared-memory block/byte capacity usage. Spool Pending displays batches and bytes, not an invented row count. Current EPS comes from consecutive receiver snapshots. Uptime runs from receiver start through its latest heartbeat. Monitoring reads snapshots/API storage samples; no per-packet work, new queue, broker or database is introduced.

## Validation

- Full unittest discovery: 59 tests, 58 passed. Existing unrelated test_compression.CompressionTests.test_plan_is_codec_only fails because it expects two Delta codecs while the existing schema has three. Compression implementation/schema were not changed.
- New accounting tests cover backlog, historical kernel exclusion, exact loss/denominator, stale runs, DOWN, queue/write/worker issues and unavailable kernel counters.
- Both Edge browser smoke tests pass: collapsed details, no worker cards, 16 compact rows, existing auth/device/branding/search/CSV behavior and exact ten-column contract.
- UDP integration smoke passes: single socket, two test consumers, 1205 received, two authorization denials, 1203 durably spooled; clean drain with zero application drops. ClickHouse is an isolated file sink in this test.
- Python compilation and JavaScript syntax pass. Workspace devices.db SHA256 was unchanged by tests.

Commands (from project root):

```text
python -m unittest discover -s tests -v
python tests/browser_udp_health.py
python tests/browser_smoke.py
python tests/udp_smoke.py
python -m py_compile listener.py monitoring.py
node --check static/console.js
```

## Files

listener.py (startup baseline and snapshot fields only), monitoring.py, static/index.html, static/console.js, static/console.css, tests/test_ingestion_health.py, tests/test_normalizer.py, tests/browser_udp_health.py, tests/browser_smoke.py, docs/INGESTION_HEALTH.md.

No deployment, service restart, data deletion, schema migration, credentials change or worker-count/configuration change was performed. The new baseline fields become available on the next operator-controlled listener start; an already-running older listener cannot provide an accurate historical baseline retroactively.
