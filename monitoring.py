import json
import time
import psutil
import config
import device_store
from database import get_client

def authorization_applied():
    expected = device_store.authorization_hash()
    snapshots = []
    for path in config.DATA_DIR.glob('listener-*.json'):
        try:
            snapshots.append(json.loads(path.read_text()))
        except (OSError,ValueError):
            return 'unknown: unreadable listener heartbeat'
    if not snapshots or any(time.time()-s.get('heartbeat',0)>10 for s in snapshots):
        return 'unknown: listener unavailable'
    return 'yes (application ACL)' if all(s.get('authorization_hash')==expected for s in snapshots) else 'pending listener refresh'

def ingestion_history(now):
    # Only complete, covered minutes, ending one minute behind the clock to
    # allow the listener's 10-second coverage checkpoint to finish.
    last = int(now//60)*60-120
    expected = config.NUM_WORKERS if __import__('socket').__dict__.get('SO_REUSEPORT') is not None else 1
    with device_store._conn() as c:
        coverage = dict(c.execute('SELECT minute,min(seconds) FROM ingest_coverage WHERE minute>=? GROUP BY minute HAVING count(*)>=?', (last-7*86400,expected)))
        inserts = dict(c.execute('SELECT minute,rows FROM ingest_minutes WHERE minute>=?', (last-7*86400,)))
    minutes = []
    for minute in range(last,last-7*86400,-60):
        if coverage.get(minute,0)<55:
            break
        minutes.append(minute)
    seconds = len(minutes)*60
    rows = sum(inserts.get(m,0) for m in minutes)
    confidence = 'LOW' if seconds<3600 else 'PRELIMINARY' if seconds<86400 else 'MEDIUM' if seconds<7*86400 else 'HIGH'
    result = dict(observed_seconds=seconds, observed_rows=rows, projection_confidence=confidence,
        measured_eps=rows/seconds if seconds else None,
        current_eps=inserts.get(last,0)/60 if minutes else None)
    for label,length in [('1h',60),('24h',1440),('7d',10080)]:
        result['eps_'+label] = sum(inserts.get(m,0) for m in minutes[:length])/(length*60) if len(minutes)>=length else None
    return result

def report():
    now = time.time()
    workers = []
    for path in config.DATA_DIR.glob('listener-*.json'):
        try:
            data = json.loads(path.read_text())
            data['up'] = now - data['heartbeat'] < 10
            workers.append(data)
        except (OSError, ValueError):
            continue
    result = dict(api='UP', sqlite='UP', listener='UP' if workers and all(w['up'] for w in workers) else 'DOWN',
                  workers=workers, listener_port=config.LISTEN_PORT, devices=device_store.counts(),
                  cpu_percent=psutil.cpu_percent(interval=.1), ram=psutil.virtual_memory()._asdict(),
                  load=psutil.getloadavg(), retention_target_days=config.RETENTION_DAYS,
                  budget_bytes=config.STORAGE_BUDGET_BYTES, clickhouse='DOWN', storage=None)
    try:
        client = get_client()
        parts = client.query('''SELECT table, sum(rows), sum(data_uncompressed_bytes),
          sum(data_compressed_bytes), sum(bytes_on_disk) FROM system.parts
          WHERE active AND database={db:String} GROUP BY table''', parameters={'db':config.CLICKHOUSE_DB})
        tables = [dict(zip(('table','rows','uncompressed_bytes','compressed_bytes','disk_bytes'), row)) for row in parts.result_rows]
        disks = client.query('SELECT name, free_space, total_space FROM system.disks').result_rows
        disk_bytes = sum(t['disk_bytes'] for t in tables)
        with device_store._conn() as c:
            pass  # Ingest history is evaluated independently of storage sampling.
            c.execute('INSERT OR IGNORE INTO storage_samples VALUES (?,?)',(int(now//60)*60,disk_bytes))
            c.execute('DELETE FROM storage_samples WHERE sample_time<?',(int(now)-32*86400,))
            baseline = c.execute('SELECT sample_time,disk_bytes FROM storage_samples WHERE sample_time>=? ORDER BY sample_time LIMIT 1',(int(now)-86400,)).fetchone()
        growth_seconds = now-baseline[0] if baseline else 0
        growth = disk_bytes-baseline[1] if baseline and growth_seconds>=60 else None
        history = ingestion_history(now)
        duration, eps = history['observed_seconds'], history['measured_eps']
        new = [t for t in tables if t['table'] in ('nat_sessions','nat_sessions_v2','events')]
        count = sum(t['rows'] for t in new)
        compressed = sum(t['compressed_bytes'] for t in new)
        raw = sum(t['uncompressed_bytes'] for t in new)
        per_row = compressed/count if count else None
        raw_per_row = raw/count if count else None
        daily = per_row*eps*86400 if per_row is not None and eps is not None else None
        free = sum(d[1] for d in disks)
        result.update(clickhouse='UP', storage=dict(tables=tables,
            database_disk_bytes=sum(t['disk_bytes'] for t in tables),
            measured_net_disk_growth_bytes=growth, disk_growth_observation_seconds=growth_seconds,
            measured_net_disk_growth_per_day=growth*86400/growth_seconds if growth is not None else None,
            compression_ratio=raw/compressed if compressed else None,
            compressed_bytes_per_row=per_row, uncompressed_bytes_per_row=raw_per_row,
            **history, daily_budget_bytes=config.STORAGE_BUDGET_BYTES/365,
            annual_safety_margin_bytes=config.STORAGE_BUDGET_BYTES-daily*365 if daily is not None else None,
            database_compressed_bytes=compressed, events_per_minute=eps*60 if eps is not None else None,
            events_per_day=eps*86400 if eps is not None else None,
            daily_raw_bytes=raw_per_row*eps*86400 if raw_per_row and eps is not None else None,
            estimated_daily_compressed_bytes=daily, estimated_30_day_bytes=daily*30 if daily is not None else None,
            estimated_365_day_bytes=daily*365 if daily is not None else None,
            budget_percent=daily*365/config.STORAGE_BUDGET_BYTES*100 if daily is not None else None,
            estimated_days_on_free_disk=free/daily if daily else None,
            disks=[dict(zip(('name','free_bytes','total_bytes'),d)) for d in disks],
            method='Acknowledged inserts during contiguous covered complete minutes (up to 7 days, 1?2 minute delay), multiplied by active structured-table bytes per row. Projection, not measured physical disk growth. History starts with this version; gaps reset confidence. Historical bytes/row reflect the stored traffic mix, not just the new parser. Excludes legacy ingest, backups, replicas and merge headroom.'))
    except Exception:
        result['clickhouse_error'] = 'Storage query unavailable; inspect API logs and database permissions.'
    return result

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--watch',action='store_true',help='Sample ClickHouse storage every 60 seconds')
    args=parser.parse_args()
    while True:
        print(json.dumps(report(),default=str),flush=True)
        if not args.watch:
            break
        time.sleep(60)
