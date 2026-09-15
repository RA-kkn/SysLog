# Native UDP burst reception

## Implementation

One existing IPv4 UDP socket -> SharedPacketRing -> existing parser workers -> durable SQLite spool -> ClickHouse. No new socket, broker, spool format, parser, auth, ID, schema or ClickHouse changes.

`udp_batch.c` uses Linux recvmmsg(MSG_DONTWAIT) with 64 reusable maximum-size datagram buffers by default (about 4 MiB). It encodes the existing 18-byte ring header in native code, avoiding per-packet Python address conversion, packing and ctypes structure walking. `linux_udp.py` loads the helper through ctypes (no Python extension ABI dependency). `udp_receive.py` copies each encoded packet into the existing bounded ring using `put_encoded`. The queue's existing packet and byte limits/backpressure remain in force. No ring buffer is exposed to the kernel or reused before its packet is copied.

Datagrams retain source IPv4/port, raw bytes and FIFO socket order. Each successful receive batch captures CLOCK_REALTIME once, immediately after recvmmsg returns; its packets share that server-read UTC nanosecond timestamp. This is not a kernel-arrival timestamp. Actual event timestamps remain parser-controlled. Concurrent worker completion ordering is unchanged. Empty UDP datagrams are real records, not shutdown markers.

No recvmmsg timeout argument is used: nonblocking reads plus a 10ms idle select avoid the documented timeout hang. Partial ring blocks flush at approximately 2ms under load and on idle/shutdown. Stop is checked once per batch; an already-received batch is accounted/enqueued before stopping. Unexpected processing errors account the unprocessed received batch tail in dropped_transport and fail loudly. Truncated/invalid native records never silently become partial records. Existing spool drain/retry/ack behavior is unchanged.

## Build and configuration (not executed on production)

Build on the target Linux distribution/architecture, not on Windows or a newer-glibc development host. Requires a C compiler and libc development headers (`gcc libc6-dev` on Ubuntu). No `-march=native` is used, preserving the target compiler's baseline CPU compatibility.

```sh
cc -O3 -std=c11 -Wall -Wextra -Werror -fPIC -shared -o _udp_batch.so.tmp udp_batch.c
mv _udp_batch.so.tmp _udp_batch.so
```

The temporary file and rename prevent rewriting a shared library mapped by a running process. No services are restarted by these commands. The generated binary is git-ignored; compile it again after updating the native source.

Settings (also in `deploy/udp-batch.env` and `.env.example`):

```text
UDP_RECEIVE_MODE=auto
UDP_BATCH_SIZE=64
```

`auto` uses the native helper when available and logs a fallback to recvfrom_into when unavailable/unsupported. `recvmmsg` explicitly requires native support and fails if unavailable; `recvfrom` forces the portable fallback. Batch size is validated in 1..256; start at 64. Keep your existing 16 workers, ring capacity, receive-buffer and spool settings. Do not replace production credentials or use the older four-worker deployment example. For systemd, add these two environment keys to the listener's existing environment file when an operator schedules rollout. A later operator-controlled listener restart is required to load new code/binary; none was performed here.

Receiver snapshots expose receive_mode, receive_batch_size, receive_calls and receive_max_batch. Kernel counters are never reset. Confirm the native mode in receiver.json after an eventual rollout.

## Monitoring

The summary labels packet loss as **current run**; details label RcvbufErrors as **host cumulative**, separately from current-run kernel drops. Loss remains current-run RcvbufErrors + dropped_queue + dropped_transport + processing/spool drops. Received minus stored is never loss. Host counters include other UDP traffic in the namespace.

Small transient spool batches do not immediately degrade health: persistent pending work for 30 seconds, >=4 batches per configured worker, or >=64MiB pending does. Write/worker problems and confirmed packet loss are not delayed. The reporter samples backlog duration outside ingestion. Tiny transient growth also no longer generates the growing-spool warning.

Storage growth selects a new baseline at the latest downward sample (wipe, TTL or merge). Existing sample history is retained. Growth is Collecting until sufficient subsequent observation exists; no negative daily-growth projection is presented. This does not reset packet-loss counters, delete logs or pretend that a storage decrease is compression.

## Measured results

Local Linux WSL, Python 3.14, 16 consumers, 512-byte payloads, ring capacity 8192, effective SO_RCVBUF reported as 8MiB. These are receiver -> ring -> counter-consumer benchmarks, not full ClickHouse production throughput. Sender and receiver share the development machine; scheduling and host traffic affect results.

| Receiver | Offered EPS | Packets | Burst | Received/consumed | Queue drops | New RcvbufErrors | Receiver CPU |
|---|---:|---:|---:|---:|---:|---:|---:|
| recvfrom_into | 100,053 | 1,000,000 | 3,000 | 999,403 | 0 | 597 | 51.7% |
| native recvmmsg | 100,059 | 1,000,000 | 3,000 | 1,000,000 | 0 | 0 | 45.4% |
| native recvmmsg | 30,000 | 900,000 | 3,000 | 900,000 | 0 | 0 | 22.1% |

CPU percentage is relative to one core over the entire test including drain. Native 100k run: 4.719 CPU seconds, 10.397 seconds including drain (96,178 drained EPS). The 30k run lasted 30 seconds of sending, 30.385 including drain. The initial pure-Python ctypes batching prototype was rejected because its metadata overhead performed worse than recvfrom_into.

The JSON artifacts are under ignored test-artifacts/burst-*.json. A zero-loss local test is not a guarantee on production or at unlimited burst size. UDP can still lose packets before the socket, when CPU/kernel/ring capacity is exceeded, or during abrupt termination before durable spooling. Representative router/NIC/VM testing remains necessary. No production deployment was performed.

## Tests and reproduction

Windows full discovery: 66 tests, 60 passed, 5 Linux-only skipped, 1 pre-existing compression-test failure (expects 2 Delta codecs; existing schema has 3). No compression/schema changes were made. Linux native tests: 5 passed; Linux shared-ring tests: 8 passed. Both Edge UI smoke tests and the isolated single-socket/spool/shutdown smoke pass. Python/JS syntax and C compilation with -Wall -Wextra -Werror pass. Workspace devices.db hash unchanged.

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p test_linux_udp.py -v
python3 tests/transport_benchmark.py --mode udp --transport shared --receiver recvfrom --packets 1000000 --rate 100000 --burst 3000 --workers 16
python3 tests/transport_benchmark.py --mode udp --transport shared --receiver recvmmsg --packets 1000000 --rate 100000 --burst 3000 --workers 16
python3 tests/transport_benchmark.py --mode udp --transport shared --receiver recvmmsg --packets 900000 --rate 30000 --burst 3000 --workers 16
```

Linux syscall tests require building the helper first. Benchmarks bind an ephemeral loopback port, not production :514, and do not use the database. Full platform tests require project dependencies.

Final overload check: 600,000 packets offered unpaced at 316,481 EPS; 592,522 received/consumed, zero ring drops, 7,478 new RcvbufErrors. Drained throughput was 253,826 EPS and receiver CPU 80.0%. This explicitly demonstrates a remaining overload limit; it is not a loss-free operating point.

## Changed files

- Receiver/native transport: `udp_batch.c`, `linux_udp.py`, `udp_receive.py`, `shared_packets.py`.
- Monitoring/UI: `listener.py`, `monitoring.py`, `udp_health.py`, `static/console.js`.
- Configuration/build artifacts: `.env.example`, `.gitignore`, `deploy/udp-batch.env`.
- Tests/benchmarks: `tests/test_linux_udp.py`, `tests/test_shared_packets.py`, `tests/test_udp_receiver.py`, `tests/test_ingestion_health.py`, `tests/browser_udp_health.py`, `tests/transport_benchmark.py`.
- Documentation: `docs/UDP_BURSTS.md`.

## Git commands (not executed)

From the project root, review and then commit/push the source changes:

```sh
git diff --check
git status --short
git add .env.example .gitignore deploy/udp-batch.env docs/UDP_BURSTS.md udp_batch.c linux_udp.py udp_receive.py shared_packets.py listener.py monitoring.py udp_health.py static/console.js tests/test_linux_udp.py tests/test_shared_packets.py tests/test_udp_receiver.py tests/test_ingestion_health.py tests/browser_udp_health.py tests/transport_benchmark.py
git commit -m "Batch UDP reception with recvmmsg and improve loss monitoring"
git push origin main
```
