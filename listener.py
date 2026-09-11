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
from parser import route_syslog, NAT_COLUMNS, EVENT_COLUMNS

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('listener')
HAS_SO_REUSEPORT = hasattr(socket, 'SO_REUSEPORT')

def effective_workers():
    return config.NUM_WORKERS if HAS_SO_REUSEPORT else 1

class DurableSpool:
    def __init__(self, path, max_bytes):
        self.path, self.max_bytes = path, max_bytes
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, table_name TEXT, payload TEXT, bytes INTEGER)')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=5)
        try:
            c.execute('PRAGMA synchronous=FULL')
            with c:
                yield c
        finally:
            c.close()

    def put(self, table, rows):
        payload = json.dumps(rows, default=str, separators=(',', ':'))
        size = len(payload.encode())
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            used = c.execute('SELECT coalesce(sum(bytes),0) FROM batches').fetchone()[0]
            if used + size > self.max_bytes:
                raise BufferError('Disk spool payload limit reached')
            c.execute('INSERT INTO batches(table_name,payload,bytes) VALUES (?,?,?)', (table, payload, size))

    def peek(self):
        with self.connect() as c:
            return c.execute('SELECT id,table_name,payload FROM batches ORDER BY id LIMIT 1').fetchone()

    def ack(self, batch_id):
        with self.connect() as c:
            c.execute('DELETE FROM batches WHERE id=?', (batch_id,))

    def size(self):
        with self.connect() as c:
            return c.execute('SELECT count(*),coalesce(sum(bytes),0) FROM batches').fetchone()

class BatchInserter:
    def __init__(self, worker_id, metrics):
        self.metrics = metrics
        self.spool = DurableSpool(config.DATA_DIR / f'spool-{worker_id}.db', config.SPOOL_MAX_BYTES)
        self.stopping = threading.Event()
        self.writer = threading.Thread(target=self._write_loop, daemon=True)
        self.writer.start()

    def persist(self, table, rows):
        if not rows:
            return
        try:
            self.spool.put(table, rows)
            self.metrics['spooled'] += len(rows)
        except Exception:
            self.metrics['dropped_spool'] += len(rows)
            log.exception('event=spool_failure lost_rows=%s', len(rows))

    def _write_loop(self):
        retry = 1
        while not self.stopping.is_set():
            try:
                batch = self.spool.peek()
                if not batch:
                    self.stopping.wait(.1)
                    continue
                batch_id, table, payload = batch
                rows = json.loads(payload)
                columns = NAT_COLUMNS if table == 'nat_sessions' else EVENT_COLUMNS
                for row in rows:
                    row['timestamp'] = datetime.fromisoformat(row['timestamp'])
                    row['received_at'] = datetime.fromisoformat(row['received_at'])
                    row['record_id'] = uuid.UUID(row['record_id'])
                start = time.monotonic()
                get_client().insert(table, [[row[k] for k in columns] for row in rows], column_names=columns)
                # Ambiguous network failures can produce duplicates; do not discard
                # an unacknowledged batch. UUID remains stable across retries.
                self.spool.ack(batch_id)
                self.metrics['inserted'] += len(rows)
                try:
                    with device_store._conn() as c:
                        c.execute('INSERT INTO ingest_minutes VALUES (?,?) ON CONFLICT(minute) DO UPDATE SET rows=rows+excluded.rows',
                                  (int(time.time()//60)*60, len(rows)))
                        c.execute('DELETE FROM ingest_minutes WHERE minute < ?', (int(time.time())-172800,))
                except Exception:
                    log.exception('event=ingest_statistics_failure')
                self.metrics['last_batch_rows'] = len(rows)
                self.metrics['insert_latency_ms'] = (time.monotonic() - start) * 1000
                retry = 1
            except Exception:
                self.metrics['write_failures'] += 1
                log.exception('event=clickhouse_write_failure')
                reset_client()
                self.stopping.wait(retry)
                retry = min(retry * 2, 30)

    def stop(self):
        self.stopping.set()
        self.writer.join(timeout=35)

def worker_main(worker_id, stop=None):
    stop = stop or mp.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    metrics = Counter(received=0, parsed=0, inserted=0, denied=0, parse_failures=0,
                      dropped_queue=0, dropped_spool=0, write_failures=0, spooled=0)
    packets = queue.Queue(maxsize=config.QUEUE_SIZE)
    inserter = BatchInserter(worker_id, metrics)
    started = time.time()

    def process():
        batches = {'nat_sessions': [], 'events': []}
        observations = Counter()
        deadline = time.monotonic() + config.BATCH_MAX_SECONDS
        while not stop.is_set() or not packets.empty():
            try:
                data, ip, port, stamp = packets.get(timeout=.1)
                try:
                    if ip not in device_store.get_approved_ips():
                        metrics['denied'] += 1
                        observations[ip] += 1
                    else:
                        observations[ip] += 0
                        table, row, failed = route_syslog(data, ip, port, stamp)
                        metrics['parsed'] += 1
                        metrics['parse_failures'] += int(failed)
                        batches[table].append(row)
                except Exception:
                    metrics['dropped_processing'] += 1
                    log.exception('event=processing_failure')
                finally:
                    packets.task_done()
            except queue.Empty:
                pass
            if time.monotonic() >= deadline or sum(map(len, batches.values())) >= config.BATCH_MAX_ROWS:
                try:
                    device_store.record_observations(observations)
                except Exception:
                    metrics['device_tracking_failures'] += sum(observations.values())
                    log.exception('event=device_tracking_failure')
                observations.clear()
                for table, rows in batches.items():
                    inserter.persist(table, rows)
                    rows.clear()
                deadline = time.monotonic() + config.BATCH_MAX_SECONDS
            if len(observations) >= 4096:
                try:
                    device_store.record_observations(observations)
                except Exception:
                    log.exception('event=device_tracking_failure')
                observations.clear()
        try:
            device_store.record_observations(observations)
        except Exception:
            log.exception('event=device_tracking_failure_shutdown')
        for table, rows in batches.items():
            inserter.persist(table, rows)

    processor = threading.Thread(target=process)
    processor.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if HAS_SO_REUSEPORT:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.SOCKET_RCVBUF)
    sock.settimeout(.25)
    try:
        sock.bind((config.LISTEN_HOST, config.LISTEN_PORT))
        log.info('event=startup worker=%s udp_port=%s', worker_id, config.LISTEN_PORT)
        last_snapshot = 0
        while not stop.is_set():
            if not processor.is_alive() or not inserter.writer.is_alive():
                log.critical('event=ingestion_thread_dead')
                raise RuntimeError('Ingestion thread stopped unexpectedly')
            try:
                data, (ip, port) = sock.recvfrom(65535)
                metrics['received'] += 1
                try:
                    packets.put_nowait((data, ip, port, datetime.now(timezone.utc)))
                except queue.Full:
                    metrics['dropped_queue'] += 1
            except socket.timeout:
                pass
            if time.time() - last_snapshot >= 1:
                snapshot = dict(metrics, heartbeat=time.time(), started=started, pid=os.getpid(),
                                queue_size=packets.qsize(), port=config.LISTEN_PORT,
                                authorization_hash=device_store.authorization_hash(device_store.get_approved_ips()),
                                spool_batches=inserter.spool.size()[0], spool_bytes=inserter.spool.size()[1],
                                kernel_drops=None)
                path = config.DATA_DIR / f'listener-{worker_id}.json'
                temp = path.with_suffix('.tmp')
                temp.write_text(json.dumps(snapshot))
                temp.replace(path)
                last_snapshot = time.time()
    finally:
        stop.set()
        sock.close()
        processor.join()
        inserter.stop()
        log.info('event=shutdown worker=%s counters=%s', worker_id, dict(metrics))

def main():
    stop = mp.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if effective_workers() == 1:
        worker_main(0, stop)
        return
    workers = [mp.Process(target=worker_main, args=(i, stop)) for i in range(effective_workers())]
    for process in workers:
        process.start()
    while not stop.wait(1):
        if any(not p.is_alive() for p in workers):
            log.error('event=worker_exited')
            stop.set()
    for process in workers:
        process.join()
    if any(p.exitcode for p in workers):
        raise RuntimeError('Listener worker failed')

if __name__ == '__main__':
    main()
