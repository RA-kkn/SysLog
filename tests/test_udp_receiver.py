import json
import multiprocessing as mp
import queue
import socket
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from udp_health import parse_snmp, deltas, warnings


class UDPTests(unittest.TestCase):
    def test_kernel_counters_named_not_positional(self):
        text = 'Ip: Forwarding\nIp: 1\nUdp: OutDatagrams RcvbufErrors InErrors InDatagrams IgnoredMulti MemErrors\nUdp: 30 7 9 100 2 0\n'
        self.assertEqual(parse_snmp(text), dict(InDatagrams=100, InErrors=9, RcvbufErrors=7, IgnoredMulti=2, MemErrors=0))
        self.assertIsNone(parse_snmp('Udp: InDatagrams RcvbufErrors\nUdp: 1 2\n')['MemErrors'])
        with self.assertRaises(ValueError):
            parse_snmp('Udp: InDatagrams\nUdp: bad\n')

    def test_deltas_handle_reset_and_warnings(self):
        self.assertEqual(deltas({'RcvbufErrors':5}, {'RcvbufErrors':3}, 1), {'RcvbufErrors':2})
        self.assertEqual(deltas({'RcvbufErrors':1}, {'RcvbufErrors':3}, 1), {})
        receiver = dict(queue_size=70,queue_capacity=100,dropped_queue=1,kernel_delta={'RcvbufErrors':2})
        worker = dict(worker_id=0,spool_bytes=100,write_failures=1)
        alerts = warnings(receiver,[worker])
        self.assertEqual(len(alerts),5)
        self.assertTrue(any('RcvbufErrors' in alert for alert in alerts))

    def test_bounded_multiprocessing_queue_reports_full(self):
        from listener import enqueue_packet
        packets=mp.get_context('spawn').Queue(maxsize=2)
        metrics=Counter()
        try:
            for n in range(3):
                enqueue_packet(packets,(b'x','127.0.0.1',1,n),metrics)
            self.assertEqual((metrics['received'],metrics['queued'],metrics['dropped_queue']),(3,2,1))
            self.assertEqual(packets.get(timeout=2)[3],0)
            self.assertEqual(packets.get(timeout=2)[3],1)
        finally:
            packets.close();packets.join_thread()

    def test_internal_enqueue_failure_is_not_silent(self):
        from listener import enqueue_packet
        from unittest.mock import Mock
        packets=Mock();packets.put_nowait.side_effect=OSError('broken pipe')
        metrics=Counter()
        with self.assertRaises(OSError):enqueue_packet(packets,b'x',metrics)
        self.assertEqual(metrics['dropped_transport'],1)

    def test_receive_hot_path_enqueues_before_any_slow_work(self):
        from listener import receive_loop
        from unittest.mock import Mock
        stop=Mock();stop.is_set.side_effect=[False,True]
        sock=Mock();sock.recvfrom.return_value=(b'raw',('192.0.2.1',20))
        packets=Mock();metrics=Counter()
        with patch('listener.write_snapshot',side_effect=AssertionError('I/O in hot path')), \
             patch('listener.device_store.get_approved_ips',side_effect=AssertionError('ACL in hot path')):
            receive_loop(sock,packets,stop,metrics)
        value=packets.put_nowait.call_args.args[0]
        self.assertEqual(value[:3],(b'raw','192.0.2.1',20))
        self.assertIsNotNone(value[3].tzinfo)

    def test_new_receiver_and_workers_reconcile(self):
        import config
        from verify_ingest import workers
        import time
        with tempfile.TemporaryDirectory() as folder, patch.object(config,'DATA_DIR',Path(folder)):
            receiver=dict(heartbeat=time.time(),state='running',run_id='a',num_workers=2,pid=10,queue_size=0,received=10)
            (Path(folder)/'receiver.json').write_text(json.dumps(receiver))
            for n in range(2):
                row=dict(heartbeat=time.time(),state='running',worker_id=n,run_id='a',pid=20+n,spool_bytes=0,inserted=5)
                (Path(folder)/f'listener-{n}.json').write_text(json.dumps(row))
            snapshots=workers()
            self.assertEqual(sum(row.get('received',0) for row in snapshots.values()),10)
            self.assertEqual(sum(row.get('inserted',0) for row in snapshots.values()),10)


if __name__=='__main__':unittest.main()
