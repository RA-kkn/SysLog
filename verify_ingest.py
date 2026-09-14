"""Read-only reconciliation for a controlled, paused-before/after sender test."""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import config

COUNTERS = ('received', 'denied', 'inserted', 'dropped_queue', 'dropped_transport', 'dropped_spool', 'dropped_processing')


def workers():
    from monitoring import ingestion_snapshots
    receiver, consumers = ingestion_snapshots()
    if not receiver['up'] or len(consumers) != receiver.get('num_workers') or not all(w['up'] for w in consumers):
        raise RuntimeError('Receiver/worker heartbeat missing or stale')
    if receiver.get('queue_size') != 0 or any(w.get('spool_bytes', 0) for w in consumers):
        raise RuntimeError('Queue/spool not drained; pause sender and wait')
    return dict(receiver=receiver, **{str(w['worker_id']):w for w in consumers})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('begin', 'check'))
    parser.add_argument('baseline', type=Path)
    args = parser.parse_args()
    current = workers()
    if args.mode == 'begin':
        with args.baseline.open('x', encoding='utf-8') as output:
            json.dump(dict(at=datetime.now(timezone.utc).isoformat(), workers=current), output)
        print('Baseline saved. Send test packets, then pause sender and drain before check.')
        return
    baseline = json.loads(args.baseline.read_text())
    old = baseline['workers']
    if set(old) != set(current) or any(old[key]['pid'] != current[key]['pid'] or
            old[key].get('run_id') != current[key].get('run_id') for key in old):
        raise RuntimeError('Workers restarted/changed; start a new controlled test')
    delta = {key: sum(current[w].get(key, 0)-old[w].get(key, 0) for w in old) for key in COUNTERS}
    from database import get_client
    count, unique = get_client().query(
        'SELECT count(),uniqExact(record_id) FROM nat_sessions_v2 WHERE received_at >= {start:DateTime64(3)}',
        parameters={'start': datetime.fromisoformat(baseline['at'])},
        settings={'max_execution_time': 60},
    ).result_rows[0]
    expected = delta['received']-delta['denied']
    passed = (expected == delta['inserted'] == count == unique and
              all(delta[key] == 0 for key in COUNTERS if key.startswith('dropped_')))
    print(json.dumps(dict(counters=delta, expected_approved=expected,
                          stored_rows=count, unique_packet_ids=unique, passed=passed), indent=2))
    if not passed:
        raise SystemExit('Reconciliation failed or work still pending; inspect counters/spool and repeat check')


if __name__ == '__main__':
    main()
