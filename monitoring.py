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
            rows, first = c.execute('SELECT coalesce(sum(rows),0),min(minute) FROM ingest_minutes WHERE minute>=?', (int(now//60)*60-86400,)).fetchone()
            c.execute('INSERT OR IGNORE INTO storage_samples VALUES (?,?)',(int(now//60)*60,disk_bytes))
            c.execute('DELETE FROM storage_samples WHERE sample_time<?',(int(now)-32*86400,))
            baseline = c.execute('SELECT sample_time,disk_bytes FROM storage_samples WHERE sample_time>=? ORDER BY sample_time LIMIT 1',(int(now)-86400,)).fetchone()
        growth_seconds = now-baseline[0] if baseline else 0
        growth = disk_bytes-baseline[1] if baseline and growth_seconds>=60 else None
        duration = min(86400, now-first) if first else 0
        eps = rows / duration if duration >= 60 else None
        new = [t for t in tables if t['table'] in ('nat_sessions','events')]
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
            measured_eps=eps, observed_seconds=duration, events_per_minute=eps*60 if eps is not None else None,
            events_per_day=eps*86400 if eps is not None else None,
            daily_raw_bytes=raw_per_row*eps*86400 if raw_per_row and eps is not None else None,
            estimated_daily_compressed_bytes=daily, estimated_30_day_bytes=daily*30 if daily is not None else None,
            estimated_365_day_bytes=daily*365 if daily is not None else None,
            budget_percent=daily*365/config.STORAGE_BUDGET_BYTES*100 if daily is not None else None,
            estimated_days_on_free_disk=free/daily if daily else None,
            disks=[dict(zip(('name','free_bytes','total_bytes'),d)) for d in disks],
            method='Observed acknowledged inserts / observation time, multiplied by active structured-table bytes per row. Projection, not measured physical disk growth. Excludes legacy ingest, backups, replicas and merge headroom.'))
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
