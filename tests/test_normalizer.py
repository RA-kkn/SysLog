import base64
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from parser import route_syslog, normalize_spooled, FIELD_BITS, NAT_V2_COLUMNS
from nat_writer import insert_batch
from nat_view import DISPLAY_COLUMNS, export_value


class NormalizationTests(unittest.TestCase):
    def row(self, raw):
        table, row, failed = route_syslog(raw, '192.0.2.1', 514, datetime.now(timezone.utc))
        self.assertEqual(table, 'nat_sessions_v2')
        self.assertEqual(set(row), set(NAT_V2_COLUMNS))
        return row

    def test_unknown_and_invalid_bytes_are_lossless(self):
        for raw in (b'', b'router started\r\n', b'unknown\xff\x00\xfe'):
            row = self.row(raw)
            restored = base64.b64decode(row['raw_bytes_b64']) if row['raw_bytes_b64'] else row['raw_message'].encode()
            self.assertEqual(restored, raw)
            self.assertEqual(row['field_mask'], 0)
            self.assertEqual(row['timestamp'], row['received_at'])

    def test_partial_values_conflicts_and_zero(self):
        row = self.row(b'NAT private_ip=10.0.0.1 private_port=0 public_port=99999 proto ICMP in:<pppoe-test>')
        self.assertEqual(row['field_mask'], 3)
        self.assertEqual(row['protocol'], 'icmp')
        self.assertEqual(row['subscriber_id'], 'pppoe-test')
        row = self.row(b'NAT private_ip=10.0.0.1 private_ip=10.0.0.2')
        self.assertEqual(row['field_mask'], 0)
        row = self.row(b'error from 10.0.0.1:443')
        self.assertEqual(row['field_mask'], 0)

    def test_parser_bug_becomes_raw_record(self):
        with patch('parser.normalize_syslog', side_effect=RuntimeError('bug')):
            table, row, failed = route_syslog(b'original', '192.0.2.1', 514, datetime.now(timezone.utc))
        self.assertTrue(failed)
        self.assertEqual(row['raw_message'], 'original')
        self.assertEqual(row['parse_status'], 'error')

    def test_old_event_spool_preserves_identity(self):
        original = self.row(b'unknown')
        row = normalize_spooled('events', original)
        self.assertEqual(row['record_id'], original['record_id'])
        self.assertEqual(row['migration_source'], 'events_spool')
        self.assertEqual(row['raw_message'], 'unknown')

    def test_ack_loss_does_not_reinsert_committed_packet(self):
        row = self.row(b'unknown')
        client = Mock()
        insert_batch(client, [row])
        client.query.assert_not_called()  # Normal path has no lookup.
        client.query.return_value = SimpleNamespace(result_rows=[(row['record_id'],)])
        self.assertEqual(insert_batch(client, [row], recover=True), 0)
        self.assertEqual(client.insert.call_count, 1)

    def test_distinct_packets_have_distinct_retry_tokens(self):
        first, second = self.row(b'identical'), self.row(b'identical')
        client = Mock()
        insert_batch(client, [first]); insert_batch(client, [first]); insert_batch(client, [second])
        tokens = [call.kwargs['settings']['insert_deduplication_token'] for call in client.insert.call_args_list]
        self.assertEqual(tokens[0], tokens[1])
        self.assertNotEqual(tokens[1], tokens[2])

    def test_csv_timezone_and_contract(self):
        self.assertEqual(len(DISPLAY_COLUMNS), 9)
        self.assertEqual(export_value('timestamp', '2026-09-12T00:00:00Z'), '2026-09-12 05:00:00')
        self.assertEqual(export_value('private_port', None), '')
        self.assertEqual(export_value('private_port', 0), 0)

    def test_reconciliation_detects_loss(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path
        import verify_ingest
        with tempfile.TemporaryDirectory() as folder:
            baseline = Path(folder)/'baseline.json'
            before = {'w': dict(pid=1, received=0, denied=0, inserted=0)}
            after = {'w': dict(pid=1, received=5, denied=2, inserted=3)}
            with patch('verify_ingest.workers', return_value=before), \
                 patch('sys.argv', ['verify_ingest.py', 'begin', str(baseline)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                verify_ingest.main()
            client = Mock()
            client.query.return_value = SimpleNamespace(result_rows=[(3, 3)])
            with patch('verify_ingest.workers', return_value=after), \
                 patch('database.get_client', return_value=client), \
                 patch('sys.argv', ['verify_ingest.py', 'check', str(baseline)]), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                verify_ingest.main()
                self.assertTrue(json.loads(output.getvalue())['passed'])
                after['w']['dropped_processing'] = 1
                with self.assertRaises(SystemExit):
                    verify_ingest.main()


if __name__ == '__main__':
    unittest.main()
