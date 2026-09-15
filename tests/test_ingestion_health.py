import unittest
from monitoring import ingestion_health

class IngestionHealthTests(unittest.TestCase):
    def sample(self, **changes):
        receiver=dict(run_id='new',up=True,num_workers=1,received=1000,current_eps=25,
                      queue_size=20,queue_capacity=100,started=100,heartbeat=200,
                      kernel_udp={'RcvbufErrors':10003},kernel_run_delta={'RcvbufErrors':0})
        worker=dict(run_id='new',worker_id=0,up=True,inserted=100,spool_batches=0)
        receiver.update(changes)
        return receiver, worker

    def test_pending_is_not_loss_and_history_excluded(self):
        r,w=self.sample()
        h=ingestion_health(r,[w],now=210)
        self.assertEqual(h['packet_loss'],0)
        self.assertEqual(h['status'],'HEALTHY')
        self.assertEqual(h['uptime_seconds'],100)
        w['spool_batches']=1
        self.assertEqual(ingestion_health(r,[w])['status'],'HEALTHY')
        r['spool_backlog_seconds']=30
        self.assertEqual(ingestion_health(r,[w])['status'],'DEGRADED')

    def test_confirmed_loss_and_denominator(self):
        r,w=self.sample(dropped_queue=2,dropped_transport=3,kernel_run_delta={'RcvbufErrors':5})
        w.update(dropped_processing=7,dropped_spool=11,parse_failures=100,denied=50)
        h=ingestion_health(r,[w])
        self.assertEqual(h['packet_loss'],28)
        self.assertAlmostEqual(h['loss_percent'],28/1005*100)
        self.assertEqual(h['status'],'LOSS DETECTED')

    def test_stale_run_ignored_and_missing_worker_degraded(self):
        r,w=self.sample()
        w.update(run_id='old',inserted=999999,dropped_spool=999)
        h=ingestion_health(r,[w])
        self.assertEqual((h['stored_records'],h['packet_loss'],h['status']),(0,0,'DEGRADED'))

    def test_down_overrides_loss_but_preserves_counters(self):
        r,w=self.sample(up=False,dropped_queue=1)
        h=ingestion_health(r,[w])
        self.assertEqual((h['status'],h['packet_loss'],h['current_eps']),('DOWN',1,0))

    def test_worker_write_queue_and_database_problems(self):
        r,w=self.sample()
        for changes in ({'up':False},{'write_failures':1}):
            with self.subTest(changes=changes):
                self.assertEqual(ingestion_health(r,[dict(w,**changes)])['status'],'DEGRADED')
        r.update(queue_bytes=70,queue_byte_capacity=100)
        self.assertEqual(ingestion_health(r,[w])['status'],'DEGRADED')
        self.assertEqual(ingestion_health(r,[w],clickhouse='DOWN')['packet_loss'],0)

    def test_growth_resets_after_decrease(self):
        from monitoring import growth_baseline
        self.assertEqual(growth_baseline([(1,100),(2,120),(3,0),(4,5)]),(3,0))
        self.assertEqual(growth_baseline([(1,100),(2,120)]),(1,100))
        self.assertIsNone(growth_baseline([]))

    def test_unavailable_kernel_not_invented(self):
        r,w=self.sample(kernel_run_delta={})
        self.assertIsNone(ingestion_health(r,[w])['kernel_loss'])

if __name__=='__main__':unittest.main()
