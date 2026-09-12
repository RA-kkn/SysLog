"""Durable disjoint ID blocks. One SQLite transaction per 65,536 packet IDs."""
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
import config

BLOCK = 65536
EPOCH_MS = 1577836800000  # 2020-01-01 UTC
_local = threading.local()


@contextmanager
def connection():
    path = config.DATA_DIR / 'record-ids.db'
    c = sqlite3.connect(path, timeout=30)
    try:
        c.execute('PRAGMA synchronous=FULL')
        c.execute('CREATE TABLE IF NOT EXISTS allocator (singleton INTEGER PRIMARY KEY CHECK(singleton=1), high INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS legacy_ids (source TEXT PRIMARY KEY, id INTEGER UNIQUE NOT NULL)')
        with c:
            yield c
    finally:
        c.close()


def reserve(count=BLOCK):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT high FROM allocator WHERE singleton=1').fetchone()
        start = max(old[0] if old else 1, max(0, int(time.time()*1000)-EPOCH_MS) << 20)
        end = start+count
        if end >= 2**63:
            raise OverflowError('ID allocator exhausted; refusing ID reuse')
        c.execute('INSERT INTO allocator VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET high=excluded.high', (end,))
    return start, end


def next_id():
    identity = (os.getpid(), str(config.DATA_DIR))
    if getattr(_local, 'identity', None) != identity or _local.next >= _local.end:
        _local.next, _local.end = reserve()
        _local.identity = identity
    value = _local.next
    _local.next += 1
    return value


def legacy_ids(sources):
    """Historical UUID mapping is durable before insertion, never a UUID hash."""
    result = {}
    # Mapping writes are migration-only, not the ingestion packet path.
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT high FROM allocator WHERE singleton=1').fetchone()
        high = max(old[0] if old else 1, max(0, int(time.time()*1000)-EPOCH_MS) << 20)
        for source in sources:
            found = c.execute('SELECT id FROM legacy_ids WHERE source=?', (source,)).fetchone()
            if found:
                result[source] = found[0]
            else:
                if high >= 2**63-1:
                    raise OverflowError('ID allocator exhausted')
                result[source] = high
                c.execute('INSERT INTO legacy_ids VALUES (?,?)', (source, high))
                high += 1
        c.execute('INSERT INTO allocator VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET high=excluded.high', (high,))
    return result


def as_id(value):
    if isinstance(value, bool) or not str(value).isdecimal():
        raise ValueError('UUID/invalid ID in spool: drain with previous release before UInt64 transition')
    value = int(value)
    if not 0 < value < 2**63:
        raise ValueError('Invalid compact record ID')
    return value
