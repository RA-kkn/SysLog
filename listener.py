"""Bounded UDP receive queue -> authorization/parser -> durable batches -> ClickHouse."""

import json
import logging
import multiprocessing as mp
import os
import queue
import signal
import socket
import sqlite3
import threading
import time
import uuid

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone

from udp_receive import create_transport, receive_loop
from shared_packets import SharedPacketRing, received_datetime

import config
import device_store

from database import get_client, reset_client
from nat_writer import insert_batch
from parser import (
    route_syslog,
    normalize_spooled,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("listener")

def effective_workers():
    return config.NUM_WORKERS


# ============================================================
# DURABLE SQLITE SPOOL
# ============================================================

class DurableSpool:
    """
    Temporary durable storage between parser and ClickHouse.

    If ClickHouse is temporarily unavailable, parsed batches
    remain on disk and the writer retries them.

    This avoids dropping already-parsed logs simply because
    ClickHouse has a temporary problem.
    """

    def __init__(self, path, max_bytes):
        self.path = path
        self.max_bytes = max_bytes

        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")

            c.execute(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY,
                    table_name TEXT,
                    payload TEXT,
                    bytes INTEGER
                )
                """
            )

    @contextmanager
    def connect(self):
        c = sqlite3.connect(
            self.path,
            timeout=5,
        )

        try:
            c.execute("PRAGMA synchronous=FULL")

            with c:
                yield c

        finally:
            c.close()

    def put(self, table, rows):
        """
        Persist a complete batch before ClickHouse insertion.
        """

        payload = json.dumps(
            rows,
            default=str,
            separators=(",", ":"),
        )

        size = len(
            payload.encode("utf-8")
        )

        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")

            used = c.execute(
                """
                SELECT coalesce(sum(bytes), 0)
                FROM batches
                """
            ).fetchone()[0]

            if used + size > self.max_bytes:
                raise BufferError(
                    "Disk spool payload limit reached"
                )

            c.execute(
                """
                INSERT INTO batches(
                    table_name,
                    payload,
                    bytes
                )
                VALUES (?, ?, ?)
                """,
                (
                    table,
                    payload,
                    size,
                ),
            )

    def peek(self):
        with self.connect() as c:
            return c.execute(
                """
                SELECT
                    id,
                    table_name,
                    payload
                FROM batches
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()

    def ack(self, batch_id):
        with self.connect() as c:
            c.execute(
                """
                DELETE FROM batches
                WHERE id = ?
                """,
                (batch_id,),
            )

    def size(self):
        with self.connect() as c:
            return c.execute(
                """
                SELECT
                    count(*),
                    coalesce(sum(bytes), 0)
                FROM batches
                """
            ).fetchone()


# ============================================================
# CLICKHOUSE BATCH WRITER
# ============================================================

class BatchInserter:
    """
    Background ClickHouse writer.

    Important:
        UDP receiver does NOT perform one ClickHouse INSERT
        for every packet.

    Parsed rows are batched, persisted to the local spool,
    then inserted into ClickHouse in groups.
    """

    def __init__(
        self,
        worker_id,
        metrics,
    ):
        self.metrics = metrics

        self.spool = DurableSpool(
            config.DATA_DIR / f"spool-{worker_id}.db",
            config.SPOOL_MAX_BYTES,
        )

        with self.spool.connect() as c:
            self.recovery_ids = {r[0] for r in c.execute("SELECT id FROM batches")}
        self.stopping = threading.Event()

        self.writer = threading.Thread(
            target=self._write_loop,
            daemon=True,
            name=f"clickhouse-writer-{worker_id}",
        )

        self.writer.start()

    def persist(
        self,
        table,
        rows,
    ):
        if not rows:
            return

        try:
            self.spool.put(
                table,
                rows,
            )

            self.metrics["spooled"] += len(
                rows
            )

        except Exception:
            self.metrics[
                "dropped_spool"
            ] += len(rows)

            log.exception(
                "event=spool_failure lost_rows=%s",
                len(rows),
            )

    def _write_loop(self):
        retry = 1

        while not self.stopping.is_set():
            batch = None
            try:
                batch = self.spool.peek()

                if not batch:
                    self.stopping.wait(0.1)
                    continue

                (
                    batch_id,
                    table,
                    payload,
                ) = batch

                rows = json.loads(
                    payload
                )

                rows = [normalize_spooled(table, row) for row in rows]
                start = time.monotonic()
                insert_batch(get_client(), rows, recover=batch_id in self.recovery_ids)

                # Important:
                #
                # Only ACK/delete spool batch AFTER successful
                # ClickHouse insertion.
                #
                # Stable packet IDs and batch tokens survive retries. Recovery
                # checks are batch-level and only used after restart/failure.
                self.spool.ack(
                    batch_id
                )

                self.recovery_ids.discard(batch_id)
                self.metrics[
                    "inserted"
                ] += len(rows)

                # --------------------------------------------
                # INGESTION STATISTICS
                # --------------------------------------------

                try:
                    current_minute = (
                        int(time.time() // 60)
                        * 60
                    )

                    with device_store._conn() as c:
                        c.execute(
                            """
                            INSERT INTO ingest_minutes
                            VALUES (?, ?)
                            ON CONFLICT(minute)
                            DO UPDATE SET
                                rows = rows + excluded.rows
                            """,
                            (
                                current_minute,
                                len(rows),
                            ),
                        )

                        c.execute(
                            """
                            DELETE FROM ingest_minutes
                            WHERE minute < ?
                            """,
                            (
                                int(time.time())
                                - 8 * 86400,
                            ),
                        )

                except Exception:
                    self.metrics[
                        "statistics_failures"
                    ] += 1

                    log.exception(
                        "event=ingest_statistics_failure"
                    )

                self.metrics[
                    "last_batch_rows"
                ] = len(rows)

                self.metrics[
                    "insert_latency_ms"
                ] = (
                    time.monotonic()
                    - start
                ) * 1000

                retry = 1

            except Exception:
                if batch:
                    self.recovery_ids.add(batch[0])
                self.metrics[
                    "write_failures"
                ] += 1

                log.exception(
                    "event=clickhouse_write_failure"
                )

                reset_client()

                self.stopping.wait(
                    retry
                )

                retry = min(
                    retry * 2,
                    30,
                )

    def stop(self):
        self.stopping.set()

        self.writer.join(
            timeout=35
        )


# ============================================================
# LISTENER WORKER
# ============================================================

def worker_main(worker_id, packets, run_id):
    # Only the receiver handles shutdown signals. Consumers exit on FIFO markers,
    # after every packet published by that receiver has been consumed.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    metrics = Counter(
        consumed=0,

        parsed=0,
        nat_parsed=0,
        normalized_fallback=0,

        inserted=0,
        spooled=0,

        denied=0,

        parse_failures=0,
        write_failures=0,

        dropped_spool=0,
        dropped_processing=0,

        device_tracking_failures=0,
        statistics_failures=0,
    )

    inserter = BatchInserter(
        worker_id,
        metrics,
    )

    started = time.time()

    # ========================================================
    # PARSER / BATCH PROCESSOR THREAD
    # ========================================================

    def process():
        batches = {
            "nat_sessions_v2": [],
        }

        observations = Counter()

        deadline = (
            time.monotonic()
            + config.BATCH_MAX_SECONDS
        )

        while True:
            try:
                packet = packets.get(timeout=0.1)
                if packet is None:
                    break
                data, ip, port, received_at = packet
                metrics['consumed'] += 1
                try:
                    # ----------------------------------------
                    # DEVICE AUTHORIZATION
                    # ----------------------------------------

                    if (
                        ip
                        not in
                        device_store.get_approved_ips()
                    ):
                        metrics[
                            "denied"
                        ] += 1

                        observations[ip] += 1

                    else:
                        # Keep the device in the observation
                        # set without counting it as denied.
                        observations[ip] += 0

                        # ------------------------------------
                        # PARSE
                        # ------------------------------------
                        #
                        # received_at is SERVER RECEIVE TIME.
                        #
                        # parser.route_syslog() extracts the
                        # router/syslog packet timestamp and
                        # uses that as row["timestamp"].
                        #
                        table, row, failed = (
                            route_syslog(
                                data,
                                ip,
                                port,
                                received_datetime(received_at),
                            )
                        )

                        metrics[
                            "parsed"
                        ] += 1

                        if row["parse_status"] == "parsed":
                            metrics[
                                "nat_parsed"
                            ] += 1

                        else:
                            metrics[
                                "normalized_fallback"
                            ] += 1

                        metrics[
                            "parse_failures"
                        ] += int(failed)

                        batches[
                            table
                        ].append(row)

                except Exception:
                    metrics[
                        "dropped_processing"
                    ] += 1

                    log.exception(
                        "event=processing_failure"
                    )

            except queue.Empty:
                pass

            # -----------------------------------------------
            # BATCH FLUSH
            # -----------------------------------------------

            batch_rows = sum(
                len(rows)
                for rows
                in batches.values()
            )

            if (
                time.monotonic()
                >= deadline
                or batch_rows
                >= config.BATCH_MAX_ROWS
            ):
                # Save device observations in one operation.
                try:
                    if observations:
                        device_store.record_observations(
                            observations
                        )

                except Exception:
                    metrics[
                        "device_tracking_failures"
                    ] += sum(
                        observations.values()
                    )

                    log.exception(
                        "event=device_tracking_failure"
                    )

                observations.clear()

                # Persist each destination-table batch.
                for (
                    table,
                    rows,
                ) in batches.items():
                    if rows:
                        inserter.persist(
                            table,
                            rows,
                        )

                        rows.clear()

                deadline = (
                    time.monotonic()
                    + config.BATCH_MAX_SECONDS
                )

            # Avoid an excessively large observation map.
            if len(observations) >= 4096:
                try:
                    device_store.record_observations(
                        observations
                    )

                except Exception:
                    metrics[
                        "device_tracking_failures"
                    ] += sum(
                        observations.values()
                    )

                    log.exception(
                        "event=device_tracking_failure"
                    )

                observations.clear()

        # ====================================================
        # PROCESSOR SHUTDOWN FLUSH
        # ====================================================

        try:
            if observations:
                device_store.record_observations(
                    observations
                )

        except Exception:
            log.exception(
                "event=device_tracking_failure_shutdown"
            )

        for (
            table,
            rows,
        ) in batches.items():
            if rows:
                inserter.persist(
                    table,
                    rows,
                )

    finished = threading.Event()
    failure = []

    def run_processor():
        try:
            process()
        except BaseException as exc:
            failure.append(exc)
            log.exception('event=processor_thread_failed worker=%s', worker_id)
        finally:
            finished.set()

    processor = threading.Thread(target=run_processor, name=f'parser-{worker_id}', daemon=True)
    processor.start()
    last_coverage = time.time()

    def snapshot(state='running'):
        spool_batches, spool_bytes = inserter.spool.size()
        write_snapshot(f'listener-{worker_id}.json', dict(metrics, role='processor',
            worker_id=worker_id, run_id=run_id, pid=os.getpid(), started=started,
            heartbeat=time.time(), state=state, spool_batches=spool_batches,
            spool_bytes=spool_bytes, authorization_hash=device_store.authorization_hash()))

    try:
        while not finished.is_set():
            if not inserter.writer.is_alive():
                raise RuntimeError('ClickHouse writer thread exited unexpectedly')
            now = time.time()
            if now-last_coverage >= 10:
                try:
                    device_store.record_coverage(worker_id, last_coverage, now)
                except Exception:
                    metrics['statistics_failures'] += 1
                    log.exception('event=coverage_statistics_failure worker=%s', worker_id)
                last_coverage = now
            try:
                snapshot()
            except Exception:
                metrics['statistics_failures'] += 1
                log.exception('event=worker_heartbeat_failure worker=%s', worker_id)
            finished.wait(1)
        processor.join()
        if failure:
            raise RuntimeError('Parser stopped before completing drain') from failure[0]
    finally:
        inserter.stop()  # Any unacknowledged batches remain in this worker's spool.
        try:
            snapshot('failed' if failure or processor.is_alive() else 'stopped')
        except Exception:
            log.exception('event=final_worker_heartbeat_failure worker=%s', worker_id)
        log.info('event=shutdown worker=%s counters=%s', worker_id, dict(metrics))


def write_snapshot(name, data):
    path = config.DATA_DIR / name
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data), encoding='utf-8')
    for attempt in range(3):
        try:
            temp.replace(path)
            return
        except PermissionError:
            # Windows readers briefly block replacement. This runs only in
            # health reporting, never the receiver hot path.
            if attempt == 2:
                raise
            time.sleep(.01)


def queue_depth(packets):
    try:
        return packets.qsize()  # Approximate; never used to decide when to stop.
    except (NotImplementedError, OSError):
        return None


def enqueue_packet(packets, packet, metrics):
    metrics['received'] += 1
    try:
        packets.put_nowait(packet)
        metrics['queued'] += 1
    except queue.Full:
        metrics['dropped_queue'] += 1
    except Exception:
        metrics['dropped_transport'] += 1
        raise  # Fail loudly; do not pretend this packet was delivered.


def run_listener(stop=None, worker_target=worker_main):
    # spawn prevents consumers from inheriting sockets, DB clients or feeder threads.
    context = mp.get_context('spawn')
    stop = stop or context.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    packets = create_transport(context, config.QUEUE_SIZE)
    run_id = f'{os.getpid()}-{time.time_ns()}'
    metrics = Counter(received=0, queued=0, dropped_queue=0, dropped_transport=0,
                      statistics_failures=0, worker_failures=0)
    processes = [context.Process(target=worker_target, args=(i, packets, run_id),
                 name=f'syslog-parser-{i}') for i in range(effective_workers())]
    monitor_stop = threading.Event()
    draining = threading.Event()
    socket_info = {}
    previous = {}
    previous_workers = {}
    from udp_health import read_udp, deltas, warnings

    def feeder_error(exc, packet):
        metrics['dropped_transport'] += 1
        log.error('event=queue_feeder_failure packet_lost=1 error=%r', exc)
        stop.set()
    if not isinstance(packets, SharedPacketRing):
        packets._on_queue_feeder_error = feeder_error

    def sample(state='running'):
        nonlocal previous, previous_workers
        now = time.time()
        kernel = read_udp()
        elapsed = now-previous.get('heartbeat', now)
        delta = deltas(kernel, previous.get('kernel_udp'), elapsed)
        workers = []
        for i in range(effective_workers()):
            try:
                row = json.loads((config.DATA_DIR/f'listener-{i}.json').read_text())
                if row.get('run_id') == run_id:
                    workers.append(row)
            except (OSError, ValueError):
                pass
        row = dict(metrics, role='receiver', run_id=run_id, pid=os.getpid(),
                   heartbeat=now, state=state, queue_size=queue_depth(packets),
                   queue_capacity=config.QUEUE_SIZE, num_workers=len(processes),
                   worker_pids=[p.pid for p in processes], kernel_udp=kernel,
                   kernel_delta=delta, kernel_rates={k:v/elapsed for k,v in delta.items()} if elapsed>0 else {},
                   **socket_info)
        if isinstance(packets, SharedPacketRing):
            row.update(ipc='shared-ring',queue_bytes=packets.used_bytes(),queue_byte_capacity=packets.byte_capacity)
        else:
            row['ipc']='multiprocessing-queue'
        row['warnings'] = warnings(row, workers, previous, previous_workers)
        write_snapshot('receiver.json', row)
        previous, previous_workers = row, {str(w['worker_id']):w for w in workers}

    def monitor():
        while not monitor_stop.is_set():
            if not draining.is_set() and any(p.exitcode is not None for p in processes):
                metrics['worker_failures'] += 1
                log.error('event=consumer_exited receiver_stopping=1')
                stop.set()
            try:
                sample('draining' if draining.is_set() else 'running')
            except Exception:
                metrics['statistics_failures'] += 1
                log.exception('event=receiver_heartbeat_failure')
            monitor_stop.wait(1)

    sock = None
    reporter = None
    failed = False
    try:
        # Bind before starting consumers: a second instance must fail before
        # it can open the same spool files. spawn does not inherit this socket.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.SOCKET_RCVBUF)
        sock.settimeout(.01 if isinstance(packets,SharedPacketRing) else .25)
        sock.bind((config.LISTEN_HOST, config.LISTEN_PORT))
        socket_info.update(port=sock.getsockname()[1], requested_rcvbuf=config.SOCKET_RCVBUF,
                           effective_rcvbuf=sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
        for process in processes:
            process.start()
        reporter = threading.Thread(target=monitor, name='receiver-health', daemon=True)
        reporter.start()
        log.info('event=receiver_start pid=%s udp_port=%s consumers=%s', os.getpid(), socket_info['port'], len(processes))
        receive_loop(sock, packets, stop, metrics)
    except BaseException:
        failed = True
        log.exception('event=receiver_failed')
        raise
    finally:
        stop.set()
        draining.set()
        if sock is not None:
            sock.close()
        # Both transports preserve this producer's FIFO order. Markers are put only
        # AFTER reception ends. Consumers never use empty()/qsize() to exit.
        alive = [p for p in processes if p.pid is not None and p.is_alive()]
        if any(p.pid is not None and p.exitcode is not None for p in processes):
            failed = True  # No consumer is allowed to exit before its marker.
        sent = 0
        while sent < len(alive):
            if not any(p.is_alive() for p in alive):
                break
            try:
                packets.put(None, timeout=.2)
                sent += 1
            except queue.Full:
                continue
        for process in processes:
            if process.pid is not None:
                process.join()
        monitor_stop.set()
        if reporter:
            reporter.join()
        failed = failed or any(p.exitcode not in (None, 0) for p in processes) or bool(metrics['dropped_transport'] or metrics['worker_failures'])
        try:
            if socket_info:  # A rejected second bind must not overwrite the active receiver's heartbeat.
                sample('failed' if failed else 'stopped')
        except Exception:
            log.exception('event=final_receiver_heartbeat_failure')
        # A failed consumer may leave undeliverable pipe contents. Do not hang
        # interpreter shutdown on that feeder; report it as a failed run.
        if failed:
            packets.cancel_join_thread()
            log.error('event=incomplete_drain remaining_queue=%s', queue_depth(packets))
        packets.close()
        if not failed:
            packets.join_thread()
        log.info('event=receiver_shutdown counters=%s', dict(metrics))
    if failed:
        raise RuntimeError('Receiver/consumer failed; inspect counters and durable spools')


def main():
    run_listener()


if __name__ == '__main__':
    main()
