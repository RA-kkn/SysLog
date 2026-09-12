"""Explicit destructive operations. No caller runs these during deployment."""
import getpass
import json
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import config
import device_store

LOG_TABLES = ('nat_sessions_v2', 'nat_sessions', 'events', 'syslogs')
KARACHI = ZoneInfo('Asia/Karachi')


def audit(action, actor, details, status='requested', audit_id=None):
    # A failed audit write prevents submission. Status uncertainty is retained.
    with device_store._conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS deletion_audit (
            id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
            at TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL)''')
        if audit_id:
            c.execute('UPDATE deletion_audit SET status=?,details=? WHERE id=?',
                      (status, json.dumps(details), audit_id))
        else:
            audit_id = uuid.uuid4().hex
            c.execute('INSERT INTO deletion_audit VALUES (?,?,?,?,?,?)',
                      (audit_id, actor, action, datetime.now(timezone.utc).isoformat(), json.dumps(details), status))
    return audit_id


def time_range(start, end):
    def convert(value):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if value.tzinfo is None:
            value = value.replace(tzinfo=KARACHI)
        return value.astimezone(timezone.utc)
    try:
        start, end = convert(start), convert(end)
        if not start < end or end-start > timedelta(days=366):
            raise ValueError()
        if start.year < 1970 or end.year > 2100:
            raise ValueError()
        return start, end
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Use valid From < To, at most 366 days, years 1970–2100')


PREDICATE = 'timestamp >= {start:DateTime64(3)} AND timestamp < {end:DateTime64(3)}'


def preview(client, start, end):
    start, end = time_range(start, end)
    count = client.query('SELECT count() FROM nat_sessions_v2 WHERE '+PREDICATE,
                         parameters={'start': start, 'end': end},
                         settings={'max_execution_time': 10}).result_rows[0][0]
    return dict(count=count, start_utc=start.isoformat(), end_utc=end.isoformat())


def delete_range(client, actor, start, end, confirmation):
    details = dict(requested_from=start, requested_to=end)
    action = audit('delete-range', actor, details)
    try:
        if confirmation != 'DELETE PERMANENTLY':
            raise ValueError('Confirmation must match DELETE PERMANENTLY exactly')
        details.update(preview(client, start, end))
        # Record UTC bounds and count BEFORE issuing the mutation.
        audit('delete-range', actor, details, 'submitting', action)
        params = {'start': datetime.fromisoformat(details['start_utc']),
                  'end': datetime.fromisoformat(details['end_utc'])}
        client.command('ALTER TABLE nat_sessions_v2 DELETE WHERE '+PREDICATE,
                       parameters=params, settings={'mutations_sync': 0, 'max_execution_time': 10})
    except ValueError:
        audit('delete-range', actor, details, 'rejected', action)
        raise
    except Exception:
        # Network timeout can occur after acceptance: never claim definite failure.
        audit('delete-range', actor, details, 'failed-or-unknown; inspect system.mutations before retry', action)
        raise
    audit('delete-range', actor, details, 'accepted', action)
    return dict(details, audit_id=action, status='accepted',
                message='Deletion accepted. Background mutation is running; completion is not yet confirmed.')


def require_quiet():
    """Native single-VM maintenance only; refuse known writers and pending spool."""
    if sys.platform == 'linux':
        for service in ('syslog-console-listener', 'syslog-console-api'):
            state = subprocess.run(['systemctl', 'is-active', service], capture_output=True, text=True)
            if state.stdout.strip() not in ('inactive', 'failed', 'unknown'):
                raise RuntimeError('Stop listener and API services before maintenance')
    for path in config.DATA_DIR.glob('listener-*.json'):
        row = json.loads(path.read_text())
        if time.time()-row.get('heartbeat', 0) < 15:
            raise RuntimeError('Recent listener heartbeat; stop all senders/listeners and wait 15 seconds')
    for path in config.DATA_DIR.glob('spool-*.db'):
        c = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
        try:
            if c.execute('SELECT count() FROM batches').fetchone()[0]:
                raise RuntimeError('Spool not empty: drain with previous version; never discard it automatically')
        finally:
            c.close()


def wipe(client, confirmation):
    if confirmation != 'DELETE ALL LOG DATA':
        raise ValueError('Confirmation mismatch; nothing cleared')
    require_quiet()
    details = {'cleared': [], 'missing': []}
    actor = 'cli:'+getpass.getuser()
    action = audit('wipe-logs', actor, details)
    try:
        for table in LOG_TABLES:
            exists = client.query('SELECT count() FROM system.tables WHERE database={db:String} AND name={table:String}',
                                  parameters={'db': config.CLICKHOUSE_DB, 'table': table}).result_rows[0][0]
            if not exists:
                details['missing'].append(table)
                print('Not present:', table)
                continue
            # Identifiers come exclusively from the fixed allowlist, never user input.
            client.command(f'TRUNCATE TABLE {config.CLICKHOUSE_DB}.{table}')
            if client.query(f'SELECT count() FROM {config.CLICKHOUSE_DB}.{table}').result_rows[0][0]:
                raise RuntimeError('Table refilled; stop every writer before wiping')
            details['cleared'].append(table)
            audit('wipe-logs', actor, details, 'in-progress', action)
            print('Cleared:', table)
    except Exception:
        audit('wipe-logs', actor, details, 'failed-or-partial', action)
        raise
    audit('wipe-logs', actor, details, 'complete', action)
    return details


def migrate_id_schema(client, confirmation, backup):
    if confirmation != 'MIGRATE EMPTY NAT TABLE':
        raise ValueError('Confirmation mismatch; schema unchanged')
    require_quiet()
    db = config.CLICKHOUSE_DB
    table = db+'.nat_sessions_v2'
    if client.query('SELECT engine FROM system.databases WHERE name={db:String}', parameters={'db':db}).result_rows != [('Atomic',)]:
        raise RuntimeError('This migration requires an Atomic database')
    if client.query(f'SELECT count() FROM {table}').result_rows[0][0] != 0:
        raise RuntimeError('NAT table is not empty; migration refused')
    if client.query('SELECT count() FROM system.mutations WHERE database={db:String} AND table=\'nat_sessions_v2\' AND NOT is_done', parameters={'db':db}).result_rows[0][0]:
        raise RuntimeError('Pending mutations; wait before schema transition')
    definition = client.command(f'SHOW CREATE TABLE {table}').replace('\\n', '\n').replace("\\'", "'")
    # Preserve the actual live definition, including existing TTL/settings/indexes.
    if not re.search(r'`?record_id`?\s+UUID\b', definition):
        raise RuntimeError('Expected UUID schema; refusing ambiguous/repeated transition')
    replacement = 'nat_sessions_v2_uint64_'+uuid.uuid4().hex[:12]
    changed = re.sub(r'`?record_id`?\s+UUID(?:\s+CODEC\([^\n]*\))?',
                     'record_id UInt64 CODEC(Delta, ZSTD(9))', definition, count=1)
    changed, renamed = re.subn(r'(?m)^CREATE TABLE\s+(?:(?:`[^`]+`|[A-Za-z0-9_]+)\.)?`?nat_sessions_v2`?',
                                f'CREATE TABLE {db}.{replacement}', changed, count=1)
    if renamed != 1:
        raise RuntimeError('Could not build replacement CREATE TABLE safely')
    with Path(backup).open('x', encoding='utf-8') as output:
        json.dump(dict(original=definition, replacement=changed, archive_table=db+'.'+replacement), output, indent=2)
    actor = 'cli:'+getpass.getuser()
    details = dict(backup=str(backup), archive_table=db+'.'+replacement)
    action = audit('uuid-to-uint64', actor, details)
    try:
        client.command(changed)
        # Recheck directly before the atomic swap. All external writers must also
        # be stopped; ClickHouse cannot lock out arbitrary remote INSERT clients.
        if client.query(f'SELECT count() FROM {table}').result_rows[0][0]:
            raise RuntimeError('Rows arrived during maintenance; exchange refused')
        client.command(f'EXCHANGE TABLES {table} AND {db}.{replacement}')
        from schema_audit import validate
        validate(client)
    except Exception:
        audit('uuid-to-uint64', actor, details, 'failed-or-unknown; inspect tables', action)
        raise
    audit('uuid-to-uint64', actor, details, 'complete', action)
    print('UInt64 active. Original empty UUID table retained at', db+'.'+replacement)
    return details
