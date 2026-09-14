# Single UDP receiver

Before: NUM_WORKERS processes each owned a SO_REUSEPORT UDP socket, a private
queue, parser, spool and writer. Linux's flow hash could send all traffic from
one router flow to one socket, leaving the other workers idle.

Now: the parent owns **one UDP socket**, feeding one bounded
`multiprocessing.Queue(QUEUE_SIZE)`. NUM_WORKERS spawned consumers share that
queue; each keeps its existing parser thread, device tracking, durable SQLite
spool and ClickHouse writer. No Redis, schema, record-ID, parser, authorization,
search/export, or deletion behavior changes are part of this revision.

The parent binds before spawning consumers, so a second instance fails before
opening the same spools. The spawn start method prevents descriptor inheritance.
The receive loop only receives, captures UTC receive time, and attempts enqueue,
with lightweight counters. Heartbeats, worker checks, kernel sampling, JSON I/O,
spool inspections and SQLite coverage writes run outside that loop.

## Queue and shutdown semantics

`QUEUE_SIZE` is a **total packet bound**, not a per-worker allocation. This is
the native multiprocessing queue, not Manager.Queue. Its feeder serializes packets
to a pipe, and consumers deserialize independently. qsize is approximate and used
only for monitoring; it never controls shutdown. On a platform lacking qsize,
depth is reported unavailable rather than as a false zero.

SIGTERM/SIGINT stops reception and closes the socket. The parent then appends one
FIFO end marker per consumer, behind all accepted packets. Consumers flush their
parser batches into their original `spool-N.db` before exiting. The writer only
acknowledges after successful insertion; unacknowledged batches remain durable
for the next start. A failed consumer/queue transport is logged and the receiver
stops with a failed status. No silent successful shutdown is reported in that case.

Worker signal handling defers graceful termination to the parent. Use systemd
KillMode=mixed, with enough TimeoutStopSec for the backlog. Forced termination,
worker failure between dequeue and spooling, or power failure can lose in-memory
packets. A dead worker holding an IPC lock can also prevent full drain; the service
timeout is the final bound. Kernel/socket packets and packets sent during restart
are outside the durable spool. UDP does not acknowledge or retransmit those packets.

Spool exhaustion still invokes the existing explicit drop counters. Queue-full
packets are counted as `dropped_queue`; asynchronous feeder failures increment
`dropped_transport`, log an error and stop reception. Spool writes, retry tokens,
record IDs and ClickHouse recovery/ack logic are unchanged.

Do not reduce NUM_WORKERS until all old worker spools are drained, or spools for
removed worker numbers will remain pending. Do not run multiple independent
instances against the same DATA_DIR or database ID allocator.

## Metrics

`data/receiver.json` contains received, queued, dropped_queue, dropped_transport,
queue depth/capacity, receiver PID, worker PIDs, run identity, lifecycle state,
requested/effective socket receive buffer and kernel counters/deltas/rates.

`data/listener-N.json` now describes a **consumer**, with consumed, parsed,
NAT/fallback, denied, stored/acknowledged, spooled, backlog, write failures,
processing/spool drops, tracking failures and statistics failures. Old heartbeats
from another run are excluded from health and authorization status. Receive totals
must come from the receiver, not be multiplied/summed across consumers.

The health page displays kernel InDatagrams, InErrors, **RcvbufErrors**,
IgnoredMulti and MemErrors. These are cumulative `/proc/net/snmp` counters for
the host/network namespace, including other UDP applications. Nothing resets
them. Missing counters/platforms are reported unavailable. Positive deltas/rates
are sampled by the receiver health thread; counter resets do not become negative rates.

Warnings cover queue occupancy >=70%, new queue drops, rising RcvbufErrors,
growing per-worker spool backlog, recorded write failures, transport errors and
processing/spool drops. Sampling is approximately once per second; short warning
conditions can disappear before a manual UI refresh, while cumulative loss counters
remain visible. Capture/poll `/api/system` with the existing authenticated session
for longer-term alerting.

For a controlled, paused-sender test, the existing `verify_ingest.py begin/check`
now includes receiver and consumer snapshots and detects restarts. After draining:

`receiver.received - sum(worker.denied) == sum(worker.inserted) == database rows`

requires zero drops, no outstanding spool, no concurrent migration and no restarts.
Its stored-row query and existing caveats are unchanged. When stopping immediately,
some rows may still be in durable spool: compare inserted plus pending spool rows,
not only acknowledged rows.

## Native VM deployment

Publish this revision first, then on the VM as root:

```bash
cd /opt/SysLog
git pull --ff-only
install -m 0644 deploy/udp-receiver.env /etc/syslog-console-udp.env
install -d /etc/systemd/system/syslog-console-listener.service.d
install -m 0644 deploy/udp-receiver.conf /etc/systemd/system/syslog-console-listener.service.d/20-udp-receiver.conf
systemctl daemon-reload
systemctl restart syslog-console-listener syslog-console-api
systemctl is-active syslog-console-listener syslog-console-api
ss -lunp '( sport = :514 )'
curl --fail http://127.0.0.1:8000/health
```

The separate environment file contains only:

```text
NUM_WORKERS=4
QUEUE_SIZE=500000
BATCH_MAX_ROWS=10000
BATCH_MAX_SECONDS=0.5
SPOOL_MAX_BYTES=8589934592
SOCKET_RCVBUF=33554432
```

It is loaded after the existing service EnvironmentFile without sourcing or
changing credentials. The API reads the active consumer count from the receiver
snapshot for coverage calculation. The drop-in sets KillMode=mixed and a 300-second
stop timeout. Increase the timeout if measured drain time needs longer. Pause
routers/test senders where possible before restart; there is no lossless UDP restart.
No schema migration or upgrade/wipe command is required for this UDP revision.

Check `sysctl net.core.rmem_max`: it must allow the requested 33,554,432 bytes.
Increase it only if below that value, and persist the approved host setting through
your normal sysctl configuration. Linux normally reports twice the requested
SO_RCVBUF for accounting, so an effective 67,108,864 bytes can be correct. A requested
value alone does not prove the kernel granted it; inspect receiver.json/health.

Verify under a heavy single-router flow: exactly one :514 socket, all consumers
making progress, queue below 70%, no increases in queue drops/RcvbufErrors, and
stable or shrinking spool. Use `journalctl -u syslog-console-listener -f` for errors.
For rollback, stop/drain this listener, restore the prior code revision and remove
only this service drop-in if desired, daemon-reload and restart. Spool formats and
database schema are unchanged. Do not delete spool/config/data during rollback.

## Capacity and limits

The queue's finite packet bound is not a byte limit. 500,000 packets at 512 raw
bytes are about 244 MiB of raw payload before Python objects, serialization,
pipe copies and worker batches; at 65,507 bytes they exceed 30 GiB before overhead.
Therefore a 15 GiB VM is not guaranteed safe for a queue full of maximum-size UDP
datagrams. Measure actual payload distribution, RSS and ClickHouse RAM; lower
QUEUE_SIZE if needed. This revision does not discard oversized valid packets merely
to meet an assumed memory budget. Four 8 GiB spools allow 32 GiB of JSON payload;
SQLite/WAL overhead and ClickHouse require additional disk space.

Multiprocessing.Queue can be the next bottleneck: pickling, the single feeder,
pipe bandwidth and consumer synchronization are real costs. Enqueue speed only
measures how fast the local buffer fills, not end-to-end drain. This revision fixes
SO_REUSEPORT flow pinning, but does **not certify 100k sustained EPS**. Saturation
still causes explicit queue drops and/or kernel receive-buffer errors.

Local IPC-only measurements, 50,000 packets, 512 bytes, four consumers, capacity
500,000: Windows ~23,026 drained packets/sec (2.171 seconds); Ubuntu/WSL ~25,084/sec
(1.993 seconds), zero drops, work distributed across all four consumers. These short
tests include IPC startup, not NAT parsing, network reception or ClickHouse, and are
not production-host benchmarks. Profile a sustained representative load on the VM
before selecting a supported EPS target. No external broker was introduced.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests -p test_udp_receiver.py -v
.venv/bin/python tests/udp_smoke.py
.venv/bin/python tests/browser_udp_health.py
.venv/bin/python tests/queue_benchmark.py --packets 50000 --bytes 512 --consumers 4 --capacity 500000
.venv/bin/python -m unittest discover -s tests -v
```

Six new unit tests passed: kernel parsing/reset/deltas/warnings, bounded real
multiprocessing queue/drop count, explicit enqueue failures, minimal receiver hot
path, and receiver/consumer reconciliation. Real loopback UDP test passed with one
socket owner and two consumers prohibited from opening sockets: 1,205 received,
2 denied, 1,203 parsed/durably spooled, zero queue/processing/spool drops, shutdown
with a deliberately non-empty queue drains safely. ClickHouse insertion is replaced
by a file sink in this test; existing spool/retry unit tests also pass.

Edge health test passed for receiver counters, kernel RcvbufErrors, congestion
warnings and consumer statistics. Syntax/compile checks passed. Ubuntu/WSL IPC
benchmark and actual `/proc/net/snmp` reads passed; full UDP pipeline integration
was run on Windows, not the production VM. Linux test dependencies were unavailable.

Full suite: **46 tests, 44 passed, 2 existing failures**. A pristine HEAD copy also
fails the same tests: codec test expects 2 Delta fields but current schema has 3;
display-contract test expects 9 fields but current code has 10. The old general
browser smoke similarly stops at its 9-column assertion. These unrelated schema/UI
contracts were deliberately preserved. No production credentials or data were changed;
the workspace configuration DB hash remained unchanged during regression tests.

Changed files: `listener.py`, `monitoring.py`, `verify_ingest.py`, `static/console.js`,
`static/index.html`, `tests/udp_smoke.py`; added `udp_health.py`,
`tests/test_udp_receiver.py`, `tests/browser_udp_health.py`, `tests/queue_benchmark.py`,
`deploy/udp-receiver.conf`, `deploy/udp-receiver.env`, and this document.
