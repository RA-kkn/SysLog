import io
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

TEMP=tempfile.TemporaryDirectory()
os.environ['CONFIG_DB_PATH']=str(Path(TEMP.name)/'devices.db')
os.environ['DATA_DIR']=str(Path(TEMP.name)/'data')
os.environ['COOKIE_SECURE']='false'
from fastapi.testclient import TestClient
from PIL import Image
# Set module-level configuration explicitly even if another test imported it.
# Never allow this suite's cleanup statements to target the workspace DB.
import config
config.DB_PATH=os.environ['CONFIG_DB_PATH']
config.DATA_DIR=Path(os.environ['DATA_DIR'])
config.DATA_DIR.mkdir(parents=True,exist_ok=True)
config.COOKIE_SECURE=False
import device_store
device_store.DB_PATH=config.DB_PATH
device_store.init_db()
import auth
import search
import search_api
from parser import route_syslog
from benchmark import payload
from listener import DurableSpool, BatchInserter
from collections import Counter
import time

class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(Path(device_store.DB_PATH).parent,Path(TEMP.name))
        with device_store._conn() as c:
            for table in ('users','sessions','login_limits','devices'):
                c.execute(f'DELETE FROM {table}')
        auth.save_user('admin','Admin','test-password-123','ADMIN',True,True)
        auth.save_user('viewer','Viewer','test-password-123','VIEWER',True,False)
        self.client=TestClient(search_api.app)

    def signin(self,username='admin'):
        res=self.client.post('/api/login',json={'username':username,'password':'test-password-123'})
        self.assertEqual(res.status_code,200)
        self.client.headers['X-CSRF-Token']=res.json()['csrf']

    def test_anonymous_admin_rejected(self):
        self.assertEqual(self.client.get('/api/devices').status_code,401)
        self.assertEqual(self.client.post('/api/devices/add',data={'ip':'192.0.2.1'}).status_code,401)

    def test_viewer_cannot_approve_or_export(self):
        self.signin('viewer')
        self.assertEqual(self.client.post('/api/devices/add',data={'ip':'192.0.2.1'}).status_code,403)
        self.assertEqual(self.client.get('/api/export').status_code,403)
        self.assertEqual(self.client.get('/api/users').status_code,403)

    def test_csrf_and_logout(self):
        self.signin();token=self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.client.post('/api/devices/add',data={'ip':'192.0.2.1'}).status_code,403)
        self.client.headers['X-CSRF-Token']=token
        self.assertEqual(self.client.post('/api/logout').status_code,200)
        self.assertEqual(self.client.get('/api/me').status_code,401)

    def test_device_lifecycle(self):
        device_store.record_observations({'192.0.2.1':3})
        self.signin()
        self.assertEqual(self.client.get('/api/devices').json()['devices'][0]['status'],'pending')
        self.assertEqual(self.client.post('/api/devices/add',data={'ip':'192.0.2.1'}).status_code,200)
        self.assertIn('192.0.2.1',device_store.get_approved_ips(force=True))
        self.client.post('/api/devices/192.0.2.1/name',data={'name':'Core router'})
        self.client.post('/api/devices/192.0.2.1/block')
        self.assertNotIn('192.0.2.1',device_store.get_approved_ips(force=True))
        device_store.record_observations({'192.0.2.1':2})
        row=device_store.list_devices()[0]
        self.assertEqual((row['status'],row['name'],row['denied_attempts']),('blocked','Core router',5))
        self.assertTrue(row['first_seen']);self.assertTrue(row['last_seen'])

    def test_invalid_ip_rejected(self):
        self.signin()
        self.assertEqual(self.client.post('/api/devices/add',data={'ip':"x'); DROP TABLE devices"}).status_code,422)

    def test_last_admin_and_password_hash(self):
        self.signin()
        res=self.client.post('/api/users',json={'username':'admin','role':'VIEWER'})
        self.assertEqual(res.status_code,422)
        with device_store._conn() as c:
            hashed=c.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0]
        self.assertNotIn('test-password',hashed)
        self.assertTrue(auth.verify('test-password-123',hashed))

    def test_login_throttling(self):
        for _ in range(10):
            self.assertEqual(self.client.post('/api/login',json={'username':'admin','password':'bad'}).status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'username':'admin','password':'bad'}).status_code,429)

    def test_expired_session(self):
        self.signin()
        with device_store._conn() as c:c.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.client.get('/api/me').status_code,401)

    def test_upload_validation_and_branding(self):
        self.signin()
        self.assertEqual(self.client.post('/api/settings/branding/upload',data={'kind':'logo'},files={'file':('x.svg',b'<svg onload="alert(1)">','image/png')}).status_code,422)
        self.assertEqual(self.client.post('/api/settings/branding',json={'logo_url':'https://evil.test/a.svg'}).status_code,422)
        with tempfile.TemporaryDirectory() as folder, patch.object(search_api,'UPLOAD_DIR',Path(folder)):
            content=io.BytesIO();Image.new('RGB',(2,2)).save(content,'PNG')
            result=self.client.post('/api/settings/branding/upload',data={'kind':'logo'},files={'file':('a.html',content.getvalue(),'text/html')})
            self.assertEqual(result.status_code,200)
            self.assertTrue(result.json()['url'].endswith('.png'))
        self.assertEqual(self.client.post('/api/settings/branding',json={'product_name':'Example ISP','accent_color':'#123456','display_timezone':'Asia/Karachi'}).status_code,200)

    def test_request_body_size_limit(self):
        self.signin()
        self.assertEqual(self.client.post('/api/settings/branding',content=b'x'*(3*1024*1024+1)).status_code,413)

    def test_cross_origin_login_blocked(self):
        self.assertEqual(self.client.post('/api/login',json={'username':'admin','password':'test-password-123'},headers={'Origin':'https://other.example'}).status_code,403)

    def test_search_unavailable_does_not_break_devices(self):
        self.signin()
        with patch('search.get_client',side_effect=RuntimeError('private database error')):
            res=self.client.get('/api/search')
        self.assertEqual(res.status_code,503);self.assertNotIn('private database',res.text)
        self.assertEqual(self.client.get('/api/devices').status_code,200)

    def test_search_has_time_bounds_and_exact_ips(self):
        fake=SimpleNamespace(query=lambda sql,**kw:self.capture(sql,kw))
        with patch('search.get_client',return_value=fake):
            search.query_logs(kind='nat_sessions',keyword='100.64.0.1,443')
        self.assertIn('private_ip={term0:IPv4}',self.sql)
        self.assertIn('timestamp >=',self.sql)
        self.assertIn('LIMIT {limit:UInt32}',self.sql)
        self.assertNotIn('OFFSET',self.sql)
        self.assertEqual((self.kw['parameters']['end']-self.kw['parameters']['start']).total_seconds(),86400)

    def capture(self,sql,kw):
        self.sql,self.kw=sql,kw
        return SimpleNamespace(column_names=['total'],result_rows=[(0,)]) if 'SELECT count()' in sql else SimpleNamespace(column_names=[],result_rows=[])

    def test_cursor_equal_timestamps(self):
        stamp=datetime.now(timezone.utc)
        ids=list(range(1001, 1102))
        result=SimpleNamespace(column_names=['timestamp','record_id','kind'],result_rows=[(stamp,i,'events') for i in ids])
        with patch('search.get_client') as client:
            client.return_value.query.return_value=result
            first=search.query_logs(limit=100,include_total=False)
            self.assertTrue(first['next_cursor'])
            client.return_value.query.side_effect=lambda sql,**kw: SimpleNamespace(column_names=['total'],result_rows=[(101,)]) if 'SELECT count()' in sql else SimpleNamespace(column_names=[],result_rows=[])
            search.query_logs(limit=100,cursor=first['next_cursor'])
            sql=client.return_value.query.call_args.args[0]
            self.assertIn('(source.timestamp,source.record_id)',sql)
            with self.assertRaises(Exception):search.query_logs(keyword='changed',limit=100,cursor=first['next_cursor'])

    def test_invalid_limits_and_cursor(self):
        self.signin()
        self.assertEqual(self.client.get('/api/search?limit=-1').status_code,422)
        self.assertEqual(self.client.get('/api/search?cursor=bad').status_code,422)

    def test_csv_formula_safety(self):
        self.assertEqual(search_api.csv_value('=SUM(A1)'),"'=SUM(A1)")
        self.signin()
        response=dict(results=[{'timestamp':'2026-09-12T00:00:00Z','subscriber_id':'=1+1'}],next_cursor=None)
        with patch('search_api.query_logs',return_value=response):
            r=self.client.get('/api/export?keyword=test')
        self.assertEqual(r.status_code,200);self.assertIn("'=1+1",r.text)

class IngestionTests(unittest.TestCase):
    def test_nat_compact(self):
        table,row,failed=route_syslog(payload(1),'192.0.2.1',514,datetime.now(timezone.utc))
        self.assertEqual(table,'nat_sessions_v2');self.assertFalse(failed)
        self.assertEqual(row['raw_message'],payload(1).decode());self.assertIsInstance(row['private_port'],int)

    def test_unknown_nat_preserved(self):
        for raw in (payload(1)+b' bytes=123',payload(1).replace(b'private_port=1025',b'private_port=99999'),b'NAT invalid'):
            table,row,failed=route_syslog(raw,'192.0.2.1',514,datetime.now(timezone.utc))
            self.assertEqual(table,'nat_sessions_v2');self.assertEqual(row['raw_message'],raw.decode())

    def test_syslog_envelope_preserved(self):
        raw=b'<134>1 2026-09-11T12:00:00+05:00 router app 1 - - hello'
        table,row,_=route_syslog(raw,'192.0.2.1',514,datetime.now(timezone.utc))
        self.assertEqual(table,'nat_sessions_v2');self.assertEqual(row['timestamp'].hour,7)
        self.assertEqual(row['raw_message'],raw.decode())

    def test_spool_survives_restart_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'spool.db'
            spool=DurableSpool(path,100)
            spool.put('events',[{'a':1}])
            again=DurableSpool(path,100)
            self.assertIsNotNone(again.peek())
            with self.assertRaises(BufferError):again.put('events',[{'a':'x'*200}])
            batch=again.peek();again.ack(batch[0]);self.assertIsNone(again.peek())

    def test_writer_retries_without_discard(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(config,'DATA_DIR',Path(folder)),patch('listener.get_client') as client:
            client.return_value.insert.side_effect=[RuntimeError('offline'),None]
            client.return_value.query.return_value=SimpleNamespace(result_rows=[])
            metrics=Counter();writer=BatchInserter(0,metrics)
            table,row,_=route_syslog(payload(1),'192.0.2.1',514,datetime.now(timezone.utc))
            writer.persist(table,[row])
            deadline=time.time()+4
            while metrics['inserted']<1 and time.time()<deadline:time.sleep(.05)
            writer.stop()
            self.assertEqual(metrics['inserted'],1)
            self.assertEqual(metrics['write_failures'],1)
            self.assertIsNone(writer.spool.peek())
            calls=client.return_value.insert.call_args_list
            self.assertEqual(calls[0].args,calls[1].args)

if __name__=='__main__':unittest.main()
