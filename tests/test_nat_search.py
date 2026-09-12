import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from parser import route_syslog, NAT_V2_COLUMNS

SAMPLE = ('firewall,info forward: in:<pppoe-S-jameel> out:vlan2436, '
          'connection-state:new,snat proto TCP (ACK,RST), '
          '100.68.180.201:60734->57.144.149.32:443, '
          'NAT (100.68.180.201:60734->103.125.177.119:60734)->57.144.149.32:443, len 52')


class NatTests(unittest.TestCase):
    def route(self, text):
        return route_syslog(text.encode(), '192.0.2.10', 12345, datetime.now(timezone.utc))

    def test_variations_and_diagnostics(self):
        for text in (SAMPLE, SAMPLE.replace('new,snat', 'established,snat src-mac aa:bb:cc:dd:ee:ff,'),
                     SAMPLE.replace('connection-state:new,snat ', ''),
                     SAMPLE.replace('TCP (ACK,RST)', 'UDP'), SAMPLE.replace(', len 52', ''),
                     '<134>Sep 12 12:00:00 router '+SAMPLE,
                     '<134>1 2026-09-12T12:00:00+05:00 router firewall - - - '+SAMPLE):
            with self.subTest(text=text):
                table, row, failed = self.route(text)
                self.assertEqual(table, 'nat_sessions_v2')
                self.assertFalse(failed)
                self.assertEqual(set(row), set(NAT_V2_COLUMNS))
                self.assertEqual(row['private_ip'], '100.68.180.201')
                self.assertEqual(row['public_ip'], '103.125.177.119')
                self.assertEqual(row['router_ip'], '192.0.2.10')
                self.assertEqual(row['source_port'], 12345)
                self.assertEqual(row['destination_port'], 443)
                self.assertEqual(row['subscriber_id'], 'pppoe-S-jameel')
                if 'src-mac' in text:
                    self.assertIn('aa:bb:cc:dd:ee:ff', row['syslog_prefix'])

    def test_unsafe_or_incomplete_is_preserved(self):
        for text in (SAMPLE.replace('60734','99999'), SAMPLE+' unknown=important',
                     SAMPLE.replace('100.68.180.201:60734->103.', '100.68.180.202:60734->103.'),
                     SAMPLE.replace('new,snat','new,snat,dnat'), SAMPLE.replace('TCP','ICMP')):
            table,row,failed=self.route(text)
            self.assertEqual(table,'events')
            self.assertEqual(row['raw_message'],text)
            self.assertEqual(row['event_type'],'nat_unparsed')
            self.assertTrue(failed)

    def test_packets_are_not_silently_deduplicated(self):
        first,second=self.route(SAMPLE)[1],self.route(SAMPLE)[1]
        self.assertNotEqual(first['record_id'],second['record_id'])

    def test_total_has_no_cursor_and_all_includes_events(self):
        import search
        captured=[]
        def query(sql,**kw):
            captured.append((sql,kw))
            if sql.startswith('SELECT sum(matches)'):
                return SimpleNamespace(result_rows=[(7001,)],column_names=['total'])
            return SimpleNamespace(result_rows=[],column_names=[])
        with patch('search.get_client',return_value=SimpleNamespace(query=query)):
            result=search.query_logs(kind='all',keyword='pppoe-S-jameel,443')
        self.assertEqual(result['total_count'],7001)
        self.assertEqual(result['count'],0)
        for sql,kw in captured:
            self.assertIn('FROM events',sql)
            self.assertIn('FROM nat_sessions_v2',sql)
            self.assertEqual(kw['parameters']['term1'],443)
            self.assertEqual(kw['parameters']['event_term1'],'443')
        self.assertNotIn('LIMIT',captured[0][0])


if __name__=='__main__':
    unittest.main()
