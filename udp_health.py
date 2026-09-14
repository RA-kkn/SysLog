"""Linux host/network-namespace UDP counters; never resets kernel statistics."""
from pathlib import Path

UDP_FIELDS = ('InDatagrams', 'InErrors', 'RcvbufErrors', 'IgnoredMulti', 'MemErrors')


def parse_snmp(text):
    lines = [line.split()[1:] for line in text.splitlines() if line.startswith('Udp:')]
    if len(lines) != 2 or len(lines[0]) != len(lines[1]):
        raise ValueError('Missing/malformed UDP counters')
    values = dict(zip(lines[0], map(int, lines[1])))
    return {key: values.get(key) for key in UDP_FIELDS}


def read_udp():
    try:
        return parse_snmp(Path('/proc/net/snmp').read_text(encoding='ascii'))
    except (OSError, ValueError):
        return None


def deltas(current, previous, seconds):
    if not current or not previous or seconds <= 0:
        return {}
    # A reset/reboot is not a negative packet rate.
    return {key: current[key]-previous[key] for key in UDP_FIELDS
            if current.get(key) is not None and previous.get(key) is not None
            and current[key] >= previous[key]}


def warnings(receiver, workers, previous_receiver=None, previous_workers=None):
    alerts = []
    previous_receiver = previous_receiver or {}
    previous_workers = previous_workers or {}
    depth = receiver.get('queue_size')
    if depth is not None and depth >= receiver.get('queue_capacity', 1)*.7:
        alerts.append('Shared packet queue is at least 70% full')
    if receiver.get('queue_byte_capacity') and receiver.get('queue_bytes',0) >= receiver['queue_byte_capacity']*.7:
        alerts.append('Shared IPC block/byte capacity is at least 70% full')
    if receiver.get('dropped_queue', 0) > previous_receiver.get('dropped_queue', 0):
        alerts.append('New application queue drops')
    if receiver.get('dropped_transport', 0):
        alerts.append('Queue transport failed; inspect listener logs')
    if receiver.get('kernel_delta', {}).get('RcvbufErrors', 0) > 0:
        alerts.append('Kernel UDP RcvbufErrors increased (host-wide UDP receive loss)')
    for worker in workers:
        old = previous_workers.get(str(worker.get('worker_id')), {})
        if worker.get('spool_bytes', 0) > old.get('spool_bytes', 0):
            alerts.append(f"Worker {worker.get('worker_id')}: spool backlog is growing")
        if worker.get('write_failures', 0):
            alerts.append(f"Worker {worker.get('worker_id')}: ClickHouse write failures recorded")
        if worker.get('dropped_spool', 0) or worker.get('dropped_processing', 0):
            alerts.append(f"Worker {worker.get('worker_id')}: processing/spool drops recorded")
    return alerts
