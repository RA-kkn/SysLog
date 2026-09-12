import os
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import config
import log_admin
import record_ids

class Result:
    def __init__(self, rows): self.result_rows = rows

class FakeDeleteClient:
    def __init__(self): self.commands=[]
    def query(self, sql, parameters=None, settings=None):
        if sql.startswith('SELECT count() FROM nat_sessions_v2 WHERE'):
            return Result([(42,)])
        raise AssertionError(sql)
    def command(self, sql, parameters=None, settings=None):
        self.commands.append((sql, parameters, settings))

class AdminAndIdTests(unittest.TestCase):
    def test_karachi_range_becomes_utc(self):
        start,end=log_admin.time_range('2026-09-12T15:00','2026-09-12T16:00')
        self.assertEqual(start.hour,10)
        self.assertEqual(end.hour,11)
        self.assertEqual(start.tzinfo, timezone.utc)

    def test_delete_confirmation_and_parameterized_predicate(self):
        c=FakeDeleteClient()
        with patch('log_admin.audit', return_value='audit1'):
            with self.assertRaises(ValueError):
                log_admin.delete_range(c,'admin','2026-09-12T15:00','2026-09-12T16:00','wrong')
            out=log_admin.delete_range(c,'admin','2026-09-12T15:00','2026-09-12T16:00','DELETE PERMANENTLY')
        self.assertEqual(out['count'],42)
        sql,params,_=c.commands[0]
        self.assertIn('{start:DateTime64(3)}',sql)
        self.assertNotIn('2026-09-12',sql)
        self.assertIn('start',params)

    def test_durable_ranges_do_not_reuse(self):
        with tempfile.TemporaryDirectory() as td, patch.object(config,'DATA_DIR',Path(td)):
            record_ids._local.__dict__.clear()
            a0,a1=record_ids.reserve(4)
            b0,b1=record_ids.reserve(4)
            self.assertGreaterEqual(b0,a1)
            self.assertTrue(set(range(a0,a1)).isdisjoint(range(b0,b1)))
            record_ids._local.__dict__.clear()
            x=record_ids.next_id()
            record_ids._local.__dict__.clear()
            y=record_ids.next_id()
            self.assertGreater(y,x)

    def test_as_id_rejects_uuid_spool(self):
        with self.assertRaises(ValueError): record_ids.as_id('550e8400-e29b-41d4-a716-446655440000')
        self.assertEqual(record_ids.as_id('123'),123)

if __name__=='__main__': unittest.main()
