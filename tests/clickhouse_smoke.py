"""Isolated real ClickHouse test. Creates and RETAINS test data; no destructive SQL."""
import sys
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from parser import route_syslog, NAT_COLUMNS, NAT_V2_COLUMNS, EVENT_COLUMNS, parse_syslog
from test_nat_search import SAMPLE
from nat_writer import insert_batch
from nat_view import DISPLAY_FIELDS
from migrate_events import migrate
from schema_audit import validate
import config
import search
import clickhouse_connect


def main():
    if '--confirm-test-target' not in sys.argv:
        raise SystemExit('Requires --confirm-test-target on an isolated server')
    connection=dict(host=config.CLICKHOUSE_HOST,port=config.CLICKHOUSE_PORT,username=config.CLICKHOUSE_USER,password=config.CLICKHOUSE_PASSWORD)
    client=clickhouse_connect.get_client(**connection)
    database='nat_only_test_'+uuid.uuid4().hex[:12]
    schema=(config.ROOT/'schema_structured.sql').read_text(encoding='utf-8')
    schema='\n'.join(line for line in schema.splitlines() if not line.strip().startswith('--'))
    client.command(f'CREATE DATABASE {database}')
    old_definition=schema.split('CREATE TABLE IF NOT EXISTS syslog_db.nat_sessions\n',1)[1].split(';',1)[0]
    client.command(f'CREATE TABLE {database}.nat_sessions_v2\n'+old_definition)
    stamp=datetime.now(timezone.utc).replace(microsecond=0)-timedelta(minutes=2)
    def normalize(raw):
        row=route_syslog(raw,'192.0.2.10',12345,stamp)[1]
        row['record_id']=uuid.UUID(row['record_id'])
        return row
    old=normalize(SAMPLE.encode())
    client.insert(f'{database}.nat_sessions_v2',[[old[k] for k in NAT_COLUMNS]],column_names=NAT_COLUMNS)
    for _ in range(2):
        for statement in schema.replace('syslog_db',database).split(';'):
            if statement.strip():client.command(statement)
    mirror=(config.ROOT/'schema.sql').read_text(encoding='utf-8')
    mirror='\n'.join(line for line in mirror.splitlines() if not line.strip().startswith('--'))
    for statement in mirror.replace('syslog_db',database).split(';'):
        if statement.strip():client.command(statement)
    with patch.object(config,'CLICKHOUSE_DB',database):validate(client)
    target=clickhouse_connect.get_client(**connection,database=database)
    assert target.query('SELECT count() FROM nat_sessions_v2').result_rows[0][0]==1
    rows=[normalize(SAMPLE.replace(', len 52',', prio 7->0, len 52').encode()) for _ in range(151)]
    rows += [normalize(b'unknown\xff\x00') for _ in range(110)]
    rows += [normalize(b'NAT private_ip=0.0.0.0 private_port=0 proto ICMP')]
    insert_batch(target,rows)
    insert_batch(target,rows)  # Repeated token must not insert another batch.
    assert insert_batch(target,rows,recover=True)==0
    assert target.query('SELECT count(),uniqExact(record_id) FROM nat_sessions_v2').result_rows[0]==(263,263)
    with patch('search.get_client',return_value=target):
        page=search.query_logs(limit=100)
        seen=0;cursors=set()
        while True:
            assert page['total_count']==263
            seen+=page['count']
            assert all(tuple(row)==DISPLAY_FIELDS for row in page['results'])
            if not page['next_cursor']:break
            assert page['next_cursor'] not in cursors
            cursors.add(page['next_cursor'])
            page=search.query_logs(limit=100,cursor=page['next_cursor'])
        assert seen==263
        for term in ('100.68.180.201','103.125.177.119','pppoe-S-jameel','443','pppoe-S-jameel,443'):
            assert search.query_logs(keyword=term)['total_count']==152
        assert search.query_logs(keyword='0.0.0.0')['total_count']==1
        assert search.query_logs(port=0)['total_count']==1
        assert search.query_logs(keyword='ICMP')['results'][0]['protocol']=='ICMP'
        for kind in ('all','events','legacy'):
            try:search.query_logs(kind=kind)
            except Exception as exc:assert exc.status_code==422
            else:raise AssertionError('Deprecated kind accepted')
    # Historical source fixtures are inserted only by this test, never the listener.
    source=[]
    for raw in (SAMPLE.encode(),b'NAT private_ip=10.0.0.1 proto ICMP',b'router rebooted'):
        parsed=parse_syslog(raw,'192.0.2.10',12345,stamp)
        event=dict(timestamp=stamp,received_at=stamp,record_id=uuid.uuid4(),router_ip='192.0.2.10',source_port=12345,
                   hostname='',facility=1,severity=6,event_type='nat_unparsed',message=parsed['message'],raw_message=raw.decode())
        source.append([event[k] for k in EVENT_COLUMNS])
    target.insert('events',source,column_names=EVENT_COLUMNS)
    bounds=(stamp-timedelta(seconds=1),stamp+timedelta(seconds=1))
    dry=migrate(target,*bounds,batch_size=2)
    assert (dry['source_rows'],dry['eligible'],dry['missing'])==(3,2,2),dry
    first=migrate(target,*bounds,apply=True,batch_size=2)
    assert first['complete'] and first['inserted']==2 and first['verified']==2,first
    second=migrate(target,*bounds,apply=True,batch_size=2)
    assert second['complete'] and second['inserted']==0 and second['already_verified']==2,second
    assert target.query('SELECT count() FROM events').result_rows[0][0]==3
    assert target.query('SELECT count(),uniqExact(record_id) FROM nat_sessions_v2').result_rows[0]==(265,265)
    print(f'PASS {database}: additive upgrade twice retains old row; 262 packets -> 262 new rows; retry token/recovery no duplicates; exact totals/3-page NAT-only search; unknown defaults excluded from zero matches; migration 3 sources, 2 eligible, 2 verified, rerun inserts 0; all source data retained.')

if __name__=='__main__':main()
