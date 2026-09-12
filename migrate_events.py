"""Explicit, bounded Events migration. Never deletes or changes source rows."""
import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from parser import NAT_V2_COLUMNS, normalize_syslog
from nat_writer import insert_batch
from record_ids import legacy_ids


def stamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Use an explicit timezone, e.g. 2026-09-12T00:00:00Z')
    return value.astimezone(timezone.utc)


def candidate(source):
    raw = (source.get('raw_message') or source.get('message') or '').encode('utf-8')
    row = normalize_syslog(raw, str(source['router_ip']), source.get('source_port', 0), source['received_at'])
    # Only migrate NAT-labelled records with extracted endpoints, or fully parsed NAT.
    # Generic events remain historical Events; they are not silently reclassified.
    import re
    if row['parse_status'] != 'parsed' and not (
        (row['field_mask'] and re.search(r'\b(?:snat|nat)\b', raw.decode('utf-8'), re.I))
        or row['field_mask'] & 5 == 5  # Explicit private and public addresses.
    ):
        return None
    row['record_id'] = str(source['record_id'])
    row['migration_source'] = 'events'
    return row


def comparable(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec='milliseconds')
    return str(value)


def migrate(client, start, end, apply=False, batch_size=2000):
    params = {'start': start, 'end': end}
    where = 'timestamp >= {start:DateTime64(3)} AND timestamp < {end:DateTime64(3)}'
    total, distinct = client.query('SELECT count(),uniqExact(record_id) FROM events WHERE '+where,
                                   parameters=params).result_rows[0]
    if total != distinct:
        raise RuntimeError('Source contains duplicate UUIDs; investigate before migrating this window')
    report = dict(source_rows=total, scanned=0, eligible=0, retained_only=0,
                  already_verified=0, inserted=0, verified=0, missing=0, mismatched=0)
    cursor = None
    while True:
        page_where = where
        page_params = dict(params, limit=batch_size)
        if cursor:
            page_where += ' AND (timestamp, record_id) > ({cursor_time:DateTime64(3)}, {cursor_id:UUID})'
            page_params.update(cursor_time=cursor[0], cursor_id=str(cursor[1]))
        result = client.query('SELECT * FROM events WHERE '+page_where+
                              ' ORDER BY timestamp, record_id LIMIT {limit:UInt32}', parameters=page_params)
        sources = [dict(zip(result.column_names, row)) for row in result.result_rows]
        if not sources:
            break
        report['scanned'] += len(sources)
        if report['scanned'] > total:
            raise RuntimeError('Source changed or cursor did not advance; stop and investigate')
        rows = [row for source in sources if (row := candidate(source)) is not None]
        if len({str(row['record_id']) for row in rows}) != len(rows):
            raise RuntimeError('Duplicate source UUIDs; investigate before migration')
        mapping = legacy_ids(['events:'+str(row['record_id']) for row in rows])
        for row in rows:
            row['record_id'] = mapping['events:'+str(row['record_id'])]
        report['eligible'] += len(rows)
        report['retained_only'] += len(sources)-len(rows)
        def verify():
            if not rows:
                return {}, []
            result = client.query('SELECT '+','.join(NAT_V2_COLUMNS)+
                                  ' FROM nat_sessions_v2 WHERE record_id IN {ids:Array(UInt64)}',
                                  parameters={'ids': [row['record_id'] for row in rows]})
            found = {}
            for values in result.result_rows:
                row = dict(zip(result.column_names, values))
                key = str(row['record_id'])
                if key in found:
                    raise RuntimeError('Duplicate destination UUID; no source rows were deleted')
                found[key] = row
            conflicts = [row for row in rows if str(row['record_id']) in found and any(
                comparable(row[key]) != comparable(found[str(row['record_id'])][key])
                for key in NAT_V2_COLUMNS)]
            return found, conflicts
        found, conflicts = verify()
        report['mismatched'] += len(conflicts)
        if conflicts and apply:
            raise RuntimeError('Destination differs from source normalization; refusing batch insertion')
        report['already_verified'] += len(found)-len(conflicts)
        missing = [row for row in rows if str(row['record_id']) not in found]
        if apply and missing:
            report['inserted'] += insert_batch(client, missing)
            found, conflicts = verify()
            if conflicts:
                raise RuntimeError('Post-insert field verification failed')
        report['verified'] += len(found)-len(conflicts)
        report['missing'] += len(rows)-len(found)
        cursor_time = sources[-1]['timestamp']
        if cursor_time.tzinfo is None:
            cursor_time = cursor_time.replace(tzinfo=timezone.utc)
        cursor = cursor_time, sources[-1]['record_id']
    after = client.query('SELECT count() FROM events WHERE '+where, parameters=params).result_rows[0][0]
    report['source_unchanged'] = after == total == report['scanned']
    report['complete'] = report['source_unchanged'] and report['missing'] == report['mismatched'] == 0
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start', required=True, type=stamp)
    p.add_argument('--end', required=True, type=stamp)
    p.add_argument('--apply', action='store_true', help='Default is read-only verification')
    p.add_argument('--report', required=True, type=Path, help='New JSON report file; never overwrite')
    args = p.parse_args()
    if args.start >= args.end:
        p.error('start must precede end')
    import config
    from database import get_client
    from schema_audit import validate
    lock = config.DATA_DIR / 'events-migration.lock'
    # Reserve report before any insertion. A lock left by a killed process must
    # be reviewed by the operator, not automatically stolen.
    with args.report.open('x', encoding='utf-8') as output:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, str(os.getpid()).encode())
            client = get_client()
            validate(client)
            result = migrate(client, args.start, args.end, args.apply)
            json.dump(result, output, indent=2)
            print(json.dumps(result, indent=2))
            if not result['source_unchanged'] or (args.apply and not result['complete']):
                raise SystemExit('Verification incomplete; retain Events and investigate')
        finally:
            os.close(fd)
            lock.unlink()


if __name__ == '__main__':
    main()
