import unittest
from unittest.mock import patch


class HistoryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import device_store
        from pathlib import Path
        self.temp=tempfile.TemporaryDirectory()
        self.db_patch=patch.object(device_store,'DB_PATH',str(Path(self.temp.name)/'history.db'))
        self.db_patch.start()
        device_store.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_confidence_and_gaps(self):
        import device_store
        import monitoring
        import config
        now=2000000040
        last=int(now//60)*60-120
        with device_store._conn() as c:
            c.execute('DELETE FROM ingest_coverage')
            c.execute('DELETE FROM ingest_minutes')
        with patch.object(config,'NUM_WORKERS',1):
            self.assertIsNone(monitoring.ingestion_history(now)['measured_eps'])
            with device_store._conn() as c:
                for i in range(60):
                    minute=last-i*60
                    c.execute('INSERT INTO ingest_coverage VALUES (0,?,60)',(minute,))
                    c.execute('INSERT INTO ingest_minutes VALUES (?,120)',(minute,))
            result=monitoring.ingestion_history(now)
            self.assertEqual(result['projection_confidence'],'PRELIMINARY')
            self.assertEqual(result['eps_1h'],2)
            self.assertIsNone(result['eps_24h'])
            self.assertEqual(result['observed_rows'],7200)
            with device_store._conn() as c:
                c.execute('DELETE FROM ingest_coverage WHERE minute=?',(last-5*60,))
            result=monitoring.ingestion_history(now)
            self.assertEqual(result['observed_seconds'],300)
            self.assertEqual(result['projection_confidence'],'LOW')
            self.assertIsNone(result['eps_1h'])
