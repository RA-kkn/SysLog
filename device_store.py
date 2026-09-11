
import sqlite3
import threading
import time
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone

import config
DB_PATH = config.DB_PATH

_lock = threading.Lock()
_approved_cache = set()
_cache_loaded_at = 0.0
CACHE_TTL_SECONDS = 5  # listener ke liye itni jaldi refresh kaafi hai


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        with c:
            yield c
    finally:
        c.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                ip TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | blocked
                name TEXT,
                denied_attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt TEXT,
                approved_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        columns = {row[1] for row in c.execute('PRAGMA table_info(devices)')}
        for column in ('first_seen', 'last_seen'):
            if column not in columns:
                c.execute(f'ALTER TABLE devices ADD COLUMN {column} TEXT')
        c.execute('CREATE TABLE IF NOT EXISTS ingest_minutes (minute INTEGER PRIMARY KEY, rows INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS storage_samples (sample_time INTEGER PRIMARY KEY, disk_bytes INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS ingest_coverage (worker INTEGER, minute INTEGER, seconds REAL NOT NULL, PRIMARY KEY(worker,minute))')
        # defaults
        c.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('logo_url', '/static/presets/logo-default.svg')"
        )
        c.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('background_url', '/static/presets/bg-default.svg')"
        )


def _now():
    return datetime.now(timezone.utc).isoformat()


def record_attempt(ip: str):
    """Ek IP ne data bheja jo approved nahi hai - pending list mein
    daalo ya denied_attempts badhao. Listener isko call karta hai
    jab koi unknown/non-approved IP se packet aaye."""
    with _conn() as c:
        c.execute("""
            INSERT INTO devices (ip, status, denied_attempts, last_attempt)
            VALUES (?, 'pending', 1, ?)
            ON CONFLICT(ip) DO UPDATE SET
                denied_attempts = denied_attempts + 1,
                last_attempt = excluded.last_attempt
        """, (ip, _now()))


def record_observations(observations):
    """One transaction per interval, instead of one write per denied packet."""
    now = _now()
    with _conn() as c:
        for ip, denied in observations.items():
            c.execute('''INSERT INTO devices(ip,status,denied_attempts,last_attempt,first_seen,last_seen)
              VALUES (?,'pending',?,?,?,?) ON CONFLICT(ip) DO UPDATE SET
              denied_attempts=denied_attempts+excluded.denied_attempts,
              last_attempt=CASE WHEN excluded.denied_attempts>0 THEN excluded.last_attempt ELSE last_attempt END,
              first_seen=COALESCE(first_seen,excluded.first_seen), last_seen=excluded.last_seen''',
              (ip,denied,now if denied else None,now,now))


def get_approved_ips(force: bool = False) -> set:
    """In-memory cached set, listener ke liye - har packet pe DB hit
    nahi karna, warna throughput mar jayega."""
    global _approved_cache, _cache_loaded_at
    now = time.time()
    if force or (now - _cache_loaded_at) > CACHE_TTL_SECONDS:
        with _lock:
            with _conn() as c:
                rows = c.execute(
                    "SELECT ip FROM devices WHERE status = 'approved'"
                ).fetchall()
            _approved_cache = {r[0] for r in rows}
            _cache_loaded_at = now
    return _approved_cache


def authorization_hash(ips=None):
    if ips is None:
        ips = get_approved_ips(force=True)
    return hashlib.sha256('\n'.join(sorted(ips)).encode()).hexdigest()


def list_devices(status: str = None):
    query = "SELECT ip, status, name, denied_attempts, last_attempt, approved_at, first_seen, last_seen FROM devices"
    params = ()
    if status and status != "all":
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY last_attempt DESC"
    with _conn() as c:
        rows = c.execute(query, params).fetchall()
    cols = ["ip", "status", "name", "denied_attempts", "last_attempt", "approved_at", "first_seen", "last_seen"]
    return [dict(zip(cols, r)) for r in rows]


def counts():
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) FROM devices GROUP BY status"
        ).fetchall()
    result = {"approved": 0, "pending": 0, "blocked": 0}
    for status, n in rows:
        result[status] = n
    result["total"] = sum(result.values())
    return result


def set_status(ip: str, status: str):
    assert status in ("approved", "pending", "blocked")
    with _conn() as c:
        approved_at = _now() if status == "approved" else None
        c.execute("""
            INSERT INTO devices (ip, status, approved_at)
            VALUES (?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                status = excluded.status,
                approved_at = CASE WHEN excluded.status = 'approved'
                                    THEN excluded.approved_at ELSE approved_at END
        """, (ip, status, approved_at))
    get_approved_ips(force=True)  # cache turant refresh karo


def add_manual(ip: str):
    """Admin manually ek IP add kar sakta hai (bina packet aaye)."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO devices (ip, status) VALUES (?, 'pending')", (ip,)
        )


def set_name(ip: str, name: str):
    with _conn() as c:
        c.execute("UPDATE devices SET name = ? WHERE ip = ?", (name, ip))


def get_setting(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


init_db()


def record_coverage(worker, start, end):
    # Never fill a long pause/restart with invented history.
    start = max(start, end - 15)
    with _conn() as c:
        while start < end:
            minute = int(start // 60) * 60
            stop = min(end, minute + 60)
            c.execute('INSERT INTO ingest_coverage VALUES (?,?,?) ON CONFLICT(worker,minute) DO UPDATE SET seconds=min(60,seconds+excluded.seconds)', (worker, minute, stop-start))
            start = stop
        c.execute('DELETE FROM ingest_coverage WHERE minute<?', (int(end)-8*86400,))
