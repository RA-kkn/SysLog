"""Opt-in integration test. Creates an isolated DB and retains it for inspection."""
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import route_syslog, NAT_COLUMNS, NAT_V2_COLUMNS, EVENT_COLUMNS
from test_nat_search import SAMPLE
import config
import search
from schema_audit import validate
import clickhouse_connect


def main():
    if '--confirm-test-target' not in sys.argv:
        raise SystemExit('Requires --confirm-test-target on an isolated server')
    client=clickhouse_connect.get_client(host=config.CLICKHOUSE_HOST,port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER,password=config.CLICKHOUSE_PASSWORD)
    database='search_test_'+uuid.uuid4().hex[:12]
    schema=(config.ROOT/'schema_structured.sql').read_text(encoding='utf-8')
    schema='\n'.join(line for line in schema.splitlines() if not line.strip().startswith('--'))
    client.command(f'CREATE DATABASE {database}')
    old_definition=schema.split('CREATE TABLE IF NOT EXISTS syslog_db.nat_sessions\n',1)[1].split(';',1)[0]
    client.command(f'CREATE TABLE {database}.nat_sessions_v2\n'+old_definition)
    _,old_row,_=route_syslog(SAMPLE.encode(),'192.0.2.10',12345,datetime.now(timezone.utc)-timedelta(minutes=2))
    old_row['record_id']=uuid.UUID(old_row['record_id'])
    client.insert(f'{database}.nat_sessions_v2',[[old_row[k] for k in NAT_COLUMNS]],column_names=NAT_COLUMNS)
    for _ in range(2):
        for statement in schema.replace('syslog_db',database).split(';'):
            if statement.strip(): client.command(statement)
    assert client.query(f'SELECT count() FROM {database}.nat_sessions_v2').result_rows[0][0]==1

    with patch.object(config,'CLICKHOUSE_DB',database): validate(client)
    target=clickhouse_connect.get_client(host=config.CLICKHOUSE_HOST,port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER,password=config.CLICKHOUSE_PASSWORD,database=database)
    stamp=datetime.now(timezone.utc)-timedelta(minutes=1)
    for raw,n,columns in [(SAMPLE.encode(),151,NAT_V2_COLUMNS),(b'<134>1 2026-09-12T00:00:00Z router app - - - normal event 443',110,EVENT_COLUMNS)]:
        rows=[]
        for i in range(n):
            table,row,_=route_syslog(raw,'192.0.2.10',12345,stamp)
            row['timestamp']=stamp;row['record_id']=uuid.UUID(row['record_id'])
            rows.append([row[k] for k in columns])
        target.insert(table,rows,column_names=columns)
    with patch('search.get_client',return_value=target):
        first=search.query_logs(limit=100)
        assert first['total_count']==262 and first['count']==100, first
        ids=set()
        page=first
        kinds=set()
        while True:
            assert page['total_count']==262
            for row in page['results']:
                assert row['record_id'] not in ids
                ids.add(row['record_id']);kinds.add(row['kind'])
            if not page['next_cursor']:break
            page=search.query_logs(limit=100,cursor=page['next_cursor'])
        assert len(ids)==262 and kinds=={'nat_sessions_v2','events'}
        for term in ('100.68.180.201','103.125.177.119','pppoe-S-jameel','443','pppoe-S-jameel,443'):
            result=search.query_logs(kind='nat_sessions',keyword=term)
            assert result['total_count']==152, (term,result)
            assert result['results'][0]['tcp_flags']=='ACK,RST'
            assert result['results'][0]['source_port']==12345
        assert search.query_logs(keyword='443')['total_count']==262
        assert search.query_logs(keyword='missing')['total_count']==0
        assert search.query_logs(kind='events')['total_count']==110
    import compression
    import tempfile
    with tempfile.TemporaryDirectory() as folder, patch.object(config,'CLICKHOUSE_DB',database), patch('compression.get_client',return_value=target), patch('schema_audit.get_client',return_value=target):
        compression.apply(str(Path(folder)/'before-codecs.json'))
        assert target.query('SELECT count() FROM nat_sessions_v2').result_rows[0][0]==152
        assert (Path(folder)/'before-codecs.json').exists()
    print('PASS: real codec ALTER to ZSTD(9), saved schema, verified codecs, 152 NAT rows preserved')
    print(f'PASS: {database}: 262 real ClickHouse rows, exact totals across 3 pages, both kinds, five NAT searches, numeric mixed search, diagnostics, no duplicates skipped, 12-column upgrade applied twice preserves old row. Test tables retained.')


if __name__=='__main__': main()
