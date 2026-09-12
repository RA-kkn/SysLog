"""Bounded, opt-in synthetic MikroTik benchmark; isolated tables retained for review."""
import argparse
import json
import random
import re
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import clickhouse_connect
import config
from parser import route_syslog, parse_syslog, NAT_V2_COLUMNS, EVENT_COLUMNS


def sample(i, rng):
    private = f'100.68.{rng.randrange(16)}.{rng.randrange(1,255)}'
    public = f'103.125.177.{119+i%4}'
    destination = f'57.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1,255)}'
    port, translated = rng.randrange(1024,65536), rng.randrange(1024,65536)
    dest_port = rng.choice([443,443,443,80,53,1883,5060])
    protocol = 'UDP' if dest_port in (53,5060) else 'TCP (ACK,RST)'
    return (f'firewall,info forward: in:<pppoe-user-{i%2000}> out:vlan2436, '
            f'connection-state:new,snat proto {protocol}, {private}:{port}->{destination}:{dest_port}, '
            f'NAT ({private}:{port}->{public}:{translated})->{destination}:{dest_port}, len {rng.choice([52,60,512,1500])}').encode()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rows',type=int,default=100000)
    p.add_argument('--confirm-test-target',action='store_true')
    p.add_argument('--output',default='test-artifacts/codec-results.json')
    args=p.parse_args()
    if not args.confirm_test_target or not 1000<=args.rows<=100000:
        p.error('Use --confirm-test-target with 1000..100000 rows on a test server')
    client=clickhouse_connect.get_client(host=config.CLICKHOUSE_HOST,port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER,password=config.CLICKHOUSE_PASSWORD,secure=config.CLICKHOUSE_SECURE)
    database='codec_benchmark_'+uuid.uuid4().hex[:12]
    schema=(config.ROOT/'schema_structured.sql').read_text(encoding='utf-8')
    client.command(f'CREATE DATABASE {database}')
    rng=random.Random(20260911); now=datetime.now(timezone.utc)
    nat_rows=[]; event_rows=[]
    for i in range(args.rows):
        raw=sample(i,rng); stamp=now-timedelta(seconds=i/10)
        table,row,failed=route_syslog(raw,'192.0.2.1',514,stamp)
        assert table=='nat_sessions_v2' and not failed
        row['record_id']=uuid.UUID(row['record_id']);nat_rows.append([row[k] for k in NAT_V2_COLUMNS])
        old=parse_syslog(raw,'192.0.2.1',514,stamp)
        event={**row,**old,'event_type':'pppoe','timestamp':stamp,'router_ip':'192.0.2.1'}
        event_rows.append([event[k] for k in EVENT_COLUMNS])
    result={'database':database,'server_version':client.command('SELECT version()'),
        'dataset':'Synthetic MikroTik SNAT packet logs; seeded variable IPs/ports, 2000 subscribers. Not production measurements.',
        'observed_production_eps':None,'production_observation_seconds':0,'projection_confidence':'LOW','results':[]}
    for table,source,codec,rows,columns in [('before_events','events','ZSTD(1)',event_rows,EVENT_COLUMNS)]+[(f'nat_zstd{level}','nat_sessions_v2',f'ZSTD({level})',nat_rows,NAT_V2_COLUMNS) for level in (1,3,6,9)]:
        base=schema.split(f'CREATE TABLE IF NOT EXISTS syslog_db.{source}\n',1)[1].split(';',1)[0]
        client.command(f'CREATE TABLE {database}.{table}\n'+re.sub(r'ZSTD\(\d+\)',codec,base))
        tag=f'{database}.{table}'
        started=time.perf_counter();cpu=time.process_time()
        for offset in range(0,args.rows,2000):
            client.insert(tag,rows[offset:offset+2000],column_names=columns,settings={'log_comment':tag,'log_queries':1})
        elapsed=time.perf_counter()-started; python_cpu=time.process_time()-cpu
        merged=time.perf_counter();client.command(f'OPTIMIZE TABLE {tag} FINAL'); merge_time=time.perf_counter()-merged
        storage=client.query('SELECT sum(rows),sum(data_uncompressed_bytes),sum(data_compressed_bytes) FROM system.parts WHERE active AND database={db:String} AND table={table:String}',parameters={'db':database,'table':table}).result_rows[0]
        predicate="positionCaseInsensitiveUTF8(message,'pppoe-user-1')>0 AND position(message,':443')>0" if source=='events' else "positionCaseInsensitiveUTF8(subscriber_id,'pppoe-user-1')>0 AND (private_port=443 OR public_port=443 OR destination_port=443)"
        timings=[]
        for _ in range(6):
            started=time.perf_counter();client.query(f'SELECT * FROM {tag} WHERE timestamp>=now()-INTERVAL 1 DAY AND {predicate} ORDER BY timestamp DESC LIMIT 200');timings.append((time.perf_counter()-started)*1000)
        server_cpu=None
        try:
            client.command('SYSTEM FLUSH LOGS')
            count,cpu_us=client.query("SELECT count(),sum(ProfileEvents['UserTimeMicroseconds']+ProfileEvents['SystemTimeMicroseconds']) FROM system.query_log WHERE type='QueryFinish' AND query_kind='Insert' AND log_comment={tag:String}",parameters={'tag':tag}).result_rows[0]
            if count:server_cpu=cpu_us/1e6
        except Exception:
            pass
        entry=dict(table=table,codec=codec,rows=storage[0],uncompressed_bytes=storage[1],compressed_bytes=storage[2],compressed_bytes_per_row=storage[2]/storage[0],compression_ratio=storage[1]/storage[2],insert_seconds=elapsed,client_cpu_seconds=python_cpu,server_insert_cpu_seconds=server_cpu,final_merge_seconds=merge_time,query_median_ms=statistics.median(timings[1:]))
        result['results'].append(entry);print(json.dumps(entry),flush=True)
    result['cardinality']=dict(zip(('rows','subscribers','protocols','routers'),client.query(f'SELECT count(),uniqExact(subscriber_id),uniqExact(protocol),uniqExact(router_ip) FROM {database}.nat_zstd1').result_rows[0]))
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(f'Results saved: {path}. Tables retained. CPU excludes background merges; query time includes HTTP. No production EPS extrapolation.')

if __name__=='__main__':main()
