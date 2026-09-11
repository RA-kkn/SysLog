"""Opt-in isolated codec experiment. Leaves tables for review; never drops data."""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
import clickhouse_connect
import config
from benchmark import payload
from parser import route_syslog, NAT_COLUMNS

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rows',type=int,default=100000)
    p.add_argument('--confirm-test-target',action='store_true')
    args=p.parse_args()
    if not args.confirm_test_target or not 1000<=args.rows<=1000000:
        p.error('Use --confirm-test-target and 1000..1000000 rows on an isolated server')
    client=clickhouse_connect.get_client(host=config.CLICKHOUSE_HOST,port=config.CLICKHOUSE_PORT,
        username=config.CLICKHOUSE_USER,password=config.CLICKHOUSE_PASSWORD,secure=config.CLICKHOUSE_SECURE)
    database='codec_benchmark_'+uuid.uuid4().hex[:12]
    client.command(f'CREATE DATABASE {database}')
    base=(config.ROOT/'schema_structured.sql').read_text().split('CREATE TABLE IF NOT EXISTS syslog_db.nat_sessions',1)[1].split(';',1)[0]
    for codec in ('LZ4','ZSTD(1)','ZSTD(3)'):
        table='nat_'+codec.replace('(','').replace(')','').lower()
        ddl=base.replace('ZSTD(1)',codec)
        client.command(f'CREATE TABLE {database}.{table}'+ddl)
        start=time.perf_counter()
        for offset in range(0,args.rows,2000):
            rows=[]
            for i in range(offset,min(offset+2000,args.rows)):
                _,row,_=route_syslog(payload(i),'192.0.2.1',514,datetime.now(timezone.utc))
                row['record_id']=uuid.UUID(row['record_id'])
                rows.append([row[k] for k in NAT_COLUMNS])
            client.insert(f'{database}.{table}',rows,column_names=NAT_COLUMNS)
        elapsed=time.perf_counter()-start
        storage=client.query('SELECT sum(rows),sum(data_uncompressed_bytes),sum(data_compressed_bytes) FROM system.parts WHERE active AND database={db:String} AND table={table:String}',parameters={'db':database,'table':table}).result_rows[0]
        query_start=time.perf_counter()
        client.query(f"SELECT timestamp,private_ip,public_ip FROM {database}.{table} WHERE toDate(timestamp)=today() AND private_ip=toIPv4('100.64.0.1') ORDER BY timestamp DESC LIMIT 200")
        print(json.dumps(dict(database=database,table=table,codec=codec,seconds=elapsed,rows_per_second=args.rows/elapsed,
            query_seconds=time.perf_counter()-query_start,rows=storage[0],uncompressed_bytes=storage[1],compressed_bytes=storage[2],
            note='Synthetic data; results include Python generation cost; merges may still be running. Tables retained for inspection.')))

if __name__=='__main__':main()
