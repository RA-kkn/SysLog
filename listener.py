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

import config
import device_store

from database import get_client, reset_client
from parser import (
    route_syslog,
    NAT_COLUMNS,
    EVENT_COLUMNS,
    NAT_V2_COLUMNS,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("listener")

HAS_SO_REUSEPORT = hasattr(socket, "SO_REUSEPORT")


def effective_workers():
    return config.NUM_WORKERS if HAS_SO_REUSEPORT else 1


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

                columns_by_table = {
                    "nat_sessions": NAT_COLUMNS,
                    "nat_sessions_v2": NAT_V2_COLUMNS,
                    "events": EVENT_COLUMNS,
                }

                if table not in columns_by_table:
                    raise ValueError(
                        f"Unsupported destination table: {table}"
                    )

                columns = columns_by_table[
                    table
                ]

                # JSON spool serializes datetime/UUID values
                # as strings. Convert them back before
                # ClickHouse insertion.
                for row in rows:
                    row["timestamp"] = (
                        datetime.fromisoformat(
                            row["timestamp"]
                        )
                    )

                    row["received_at"] = (
                        datetime.fromisoformat(
                            row["received_at"]
                        )
                    )

                    row["record_id"] = uuid.UUID(
                        row["record_id"]
                    )

                start = time.monotonic()

                get_client().insert(
                    table,
                    [
                        [
                            row[column]
                            for column in columns
                        ]
                        for row in rows
                    ],
                    column_names=columns,
                )

                # Important:
                #
                # Only ACK/delete spool batch AFTER successful
                # ClickHouse insertion.
                #
                # An ambiguous network failure can potentially
                # result in a retry. record_id remains stable
                # across retries.
                self.spool.ack(
                    batch_id
                )

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

def worker_main(
    worker_id,
    stop=None,
):
    stop = stop or mp.Event()

    signal.signal(
        signal.SIGINT,
        lambda *_: stop.set(),
    )

    signal.signal(
        signal.SIGTERM,
        lambda *_: stop.set(),
    )

    metrics = Counter(
        received=0,
        queued=0,

        parsed=0,
        nat_parsed=0,
        events_parsed=0,

        inserted=0,
        spooled=0,

        denied=0,

        parse_failures=0,
        write_failures=0,

        dropped_queue=0,
        dropped_spool=0,
        dropped_processing=0,

        device_tracking_failures=0,
        statistics_failures=0,
    )

    # Bounded in-memory queue.
    #
    # If parser/writer cannot keep up forever, memory will
    # not grow without limit.
    packets = queue.Queue(
        maxsize=config.QUEUE_SIZE
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
            "nat_sessions": [],
            "nat_sessions_v2": [],
            "events": [],
        }

        observations = Counter()

        deadline = (
            time.monotonic()
            + config.BATCH_MAX_SECONDS
        )

        while (
            not stop.is_set()
            or not packets.empty()
        ):
            try:
                (
                    data,
                    ip,
                    port,
                    received_at,
                ) = packets.get(
                    timeout=0.1
                )

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
                                received_at,
                            )
                        )

                        metrics[
                            "parsed"
                        ] += 1

                        if table.startswith(
                            "nat_sessions"
                        ):
                            metrics[
                                "nat_parsed"
                            ] += 1

                        else:
                            metrics[
                                "events_parsed"
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

                finally:
                    packets.task_done()

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

    processor = threading.Thread(
        target=process,
        name=f"parser-{worker_id}",
    )

    processor.start()

    # ========================================================
    # UDP SOCKET
    # ========================================================

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
    )

    if HAS_SO_REUSEPORT:
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEPORT,
            1,
        )

    sock.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_RCVBUF,
        config.SOCKET_RCVBUF,
    )

    sock.settimeout(
        0.25
    )

    try:
        sock.bind(
            (
                config.LISTEN_HOST,
                config.LISTEN_PORT,
            )
        )

        log.info(
            "event=startup worker=%s udp_port=%s",
            worker_id,
            config.LISTEN_PORT,
        )

        last_snapshot = 0
        last_coverage = time.time()

        # ====================================================
        # RECEIVE LOOP
        # ====================================================

        while not stop.is_set():
            if (
                not processor.is_alive()
                or not inserter.writer.is_alive()
            ):
                log.critical(
                    "event=ingestion_thread_dead"
                )

                raise RuntimeError(
                    "Ingestion thread stopped unexpectedly"
                )

            try:
                # --------------------------------------------
                # RECEIVE UDP PACKET
                # --------------------------------------------

                data, (
                    ip,
                    port,
                ) = sock.recvfrom(
                    65535
                )

                # ============================================
                # IMPORTANT TIMESTAMP
                # ============================================
                #
                # Capture server receive timestamp IMMEDIATELY
                # after recvfrom().
                #
                # This is NOT necessarily the event timestamp.
                #
                # parser.py will extract the timestamp carried
                # inside the router/syslog packet and store:
                #
                #     timestamp   = router packet/event time
                #     received_at = this server receive time
                #
                received_at = datetime.now(
                    timezone.utc
                )

                metrics[
                    "received"
                ] += 1

                try:
                    packets.put_nowait(
                        (
                            data,
                            ip,
                            port,
                            received_at,
                        )
                    )

                    metrics[
                        "queued"
                    ] += 1

                except queue.Full:
                    # Never allow an unlimited RAM backlog.
                    metrics[
                        "dropped_queue"
                    ] += 1

            except socket.timeout:
                pass

            # -----------------------------------------------
            # CONTINUOUS OBSERVATION COVERAGE
            # -----------------------------------------------

            if (
                time.time()
                - last_coverage
                >= 10
            ):
                coverage_end = time.time()

                try:
                    device_store.record_coverage(
                        worker_id,
                        last_coverage,
                        coverage_end,
                    )

                except Exception:
                    metrics[
                        "statistics_failures"
                    ] += 1

                    log.exception(
                        "event=coverage_statistics_failure"
                    )

                last_coverage = coverage_end

            # -----------------------------------------------
            # HEALTH/METRICS SNAPSHOT
            # -----------------------------------------------

            if (
                time.time()
                - last_snapshot
                >= 1
            ):
                spool_batches, spool_bytes = (
                    inserter.spool.size()
                )

                approved_ips = (
                    device_store.get_approved_ips()
                )

                snapshot = dict(
                    metrics,

                    heartbeat=time.time(),
                    started=started,

                    pid=os.getpid(),

                    queue_size=packets.qsize(),
                    queue_capacity=config.QUEUE_SIZE,

                    port=config.LISTEN_PORT,

                    authorization_hash=(
                        device_store.authorization_hash(
                            approved_ips
                        )
                    ),

                    spool_batches=spool_batches,
                    spool_bytes=spool_bytes,

                    # Python's portable socket API does not
                    # expose Linux kernel UDP drop counters
                    # here.
                    kernel_drops=None,
                )

                path = (
                    config.DATA_DIR
                    / f"listener-{worker_id}.json"
                )

                temp = path.with_suffix(
                    ".tmp"
                )

                temp.write_text(
                    json.dumps(snapshot),
                    encoding="utf-8",
                )

                temp.replace(
                    path
                )

                last_snapshot = time.time()

    finally:
        # ====================================================
        # CLEAN SHUTDOWN
        # ====================================================

        stop.set()

        sock.close()

        # process() drains anything already in the in-memory
        # queue before exiting.
        processor.join()

        # Parsed batches are persisted before stopping writer.
        inserter.stop()

        log.info(
            "event=shutdown worker=%s counters=%s",
            worker_id,
            dict(metrics),
        )


# ============================================================
# PROCESS SUPERVISOR
# ============================================================

def main():
    stop = mp.Event()

    signal.signal(
        signal.SIGINT,
        lambda *_: stop.set(),
    )

    signal.signal(
        signal.SIGTERM,
        lambda *_: stop.set(),
    )

    workers_count = effective_workers()

    if workers_count == 1:
        worker_main(
            0,
            stop,
        )
        return

    workers = [
        mp.Process(
            target=worker_main,
            args=(
                i,
                stop,
            ),
        )
        for i in range(
            workers_count
        )
    ]

    for process in workers:
        process.start()

    while not stop.wait(1):
        if any(
            not process.is_alive()
            for process in workers
        ):
            log.error(
                "event=worker_exited"
            )

            stop.set()

    for process in workers:
        process.join()

    if any(
        process.exitcode
        for process in workers
    ):
        raise RuntimeError(
            "Listener worker failed"
        )


if __name__ == "__main__":
    main()