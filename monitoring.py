import json
import time
import psutil
import config
import device_store
from database import get_client

def ingestion_snapshots():
    now = time.time()
    try:
        receiver = json.loads((config.DATA_DIR/'receiver.json').read_text())
        receiver['up'] = now-receiver['heartbeat'] < 10 and receiver.get('state') == 'running'
    except (OSError, ValueError, KeyError):
        receiver = dict(up=False, warnings=['Receiver heartbeat unavailable'])
    workers = []
    for i in range(receiver.get('num_workers', config.NUM_WORKERS)):
        try:
            row = json.loads((config.DATA_DIR/f'listener-{i}.json').read_text())
            if not receiver.get('run_id') or row.get('run_id') != receiver['run_id']:
                continue
            row['up'] = now-row['heartbeat'] < 10 and row.get('state') == 'running'
            workers.append(row)
        except (OSError, ValueError, KeyError):
            continue
    return receiver, workers


def authorization_applied():
    expected = device_store.authorization_hash()
    receiver, snapshots = ingestion_snapshots()
    if not receiver['up'] or len(snapshots) != receiver.get('num_workers') or not all(s['up'] for s in snapshots):
        return 'unknown: listener unavailable'
    return 'yes (application ACL)' if all(s.get('authorization_hash')==expected for s in snapshots) else 'pending listener refresh'

def ingestion_history(now):
    # Only complete, covered minutes, ending one minute behind the clock to
    # allow the listener's 10-second coverage checkpoint to finish.
    last = int(now//60)*60-120
    receiver, _ = ingestion_snapshots()
    expected = receiver.get('num_workers', config.NUM_WORKERS)
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

def ingestion_health(receiver, workers, clickhouse='UP', now=None):
    """Snapshot-only accounting. A backlog is never inferred to be lost."""
    now = time.time() if now is None else now
    run_id = receiver.get('run_id')
    workers = [w for w in workers if run_id and w.get('run_id') == run_id]
    total = lambda key: sum(max(0, w.get(key, 0)) for w in workers)
    kernel = receiver.get('kernel_run_delta', {}).get('RcvbufErrors')
    application = sum(max(0, receiver.get(k, 0)) for k in ('dropped_queue', 'dropped_transport'))
    application += total('dropped_processing') + total('dropped_spool')
    loss = application + max(0, kernel or 0)
    incoming = max(0, receiver.get('received', 0))
    denominator = incoming + max(0, kernel or 0)
    depth = receiver.get('queue_size', 0) or 0
    capacity = receiver.get('queue_capacity', 0)
    byte_capacity = receiver.get('queue_byte_capacity', 0)
    byte_usage = receiver.get('queue_bytes', 0)
    usage = max(depth/capacity if capacity else 0, byte_usage/byte_capacity if byte_capacity else 0)*100
    pending = total('spool_batches')
    problems = (len(workers) != receiver.get('num_workers') or not all(w.get('up') for w in workers)
                or usage >= 70 or pending > 0 or total('write_failures') > 0
                or receiver.get('worker_failures', 0) or clickhouse != 'UP')
    status = 'DOWN' if not receiver.get('up') else 'LOSS DETECTED' if loss else 'DEGRADED' if problems else 'HEALTHY'
    return dict(status=status, incoming_packets=incoming, stored_records=total('inserted'),
                current_eps=receiver.get('current_eps') if receiver.get('up') else 0,
                packet_loss=loss, loss_percent=loss/denominator*100 if denominator else 0,
                kernel_loss=kernel, application_loss=application, queue_percent=usage,
                queue_size=depth, queue_capacity=capacity, spool_pending_batches=pending,
                spool_pending_bytes=total('spool_bytes'),
                uptime_seconds=max(0, min(now, receiver.get('heartbeat', now))-receiver['started']) if receiver.get('started') else None,
                run_id=run_id)


def report():
    now = time.time()
    receiver, workers = ingestion_snapshots()
    listener_up = receiver['up'] and len(workers)==receiver.get('num_workers') and all(w['up'] for w in workers)
    result = dict(api='UP', sqlite='UP', listener='UP' if listener_up else 'DOWN',
                  receiver=receiver, kernel_udp=receiver.get('kernel_udp'), warnings=receiver.get('warnings', []),
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
        new = [t for t in tables if t['table'] in ('nat_sessions_v2',)]
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
            total_rows=count,
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
            method='Acknowledged inserts during contiguous covered complete minutes (up to 7 days, 1-2 minute delay), multiplied by active structured-table bytes per row. Projection, not measured physical disk growth. History starts with this version; gaps reset confidence. NAT-only bytes/row include preserved raw payloads. Legacy tables are included in disk usage, not new-ingest forecasts. Excludes legacy ingest, backups, replicas and merge headroom.'))
    except Exception:
        result['clickhouse_error'] = 'Storage query unavailable; inspect API logs and database permissions.'
    result['ingestion_health'] = ingestion_health(receiver, workers, result['clickhouse'], now)
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
