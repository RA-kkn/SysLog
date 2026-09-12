"""One production table; bounded NAT-only search with exact totals and keyset paging."""
import base64
import hashlib
import ipaddress
import json
from record_ids import as_id
import time
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from database import get_client
from parser import FIELD_BITS
from nat_view import DISPLAY_FIELDS

LIMITS=(100,200,500,1000,5000)
QUERY_SETTINGS={'max_execution_time':30,'max_memory_usage':1_000_000_000}


def utc(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def query_logs(kind='nat_sessions_v2',keyword='',ip='',start=None,end=None,limit=200,cursor=None,
               port=None,severity=None,hostname='',event_type='',include_total=True):
    started=time.perf_counter()
    if kind not in ('nat_sessions_v2','nat_sessions') or limit not in LIMITS:
        raise HTTPException(422,'Only NAT search is available; invalid type or row limit')
    if severity is not None or hostname or event_type:
        raise HTTPException(422,'Event-specific filters are deprecated')
    fingerprint=hashlib.sha256(json.dumps([keyword,ip,port]).encode()).hexdigest()
    position=None
    try:
        if cursor:
            if len(cursor)>2048:raise ValueError()
            position=json.loads(base64.urlsafe_b64decode(cursor))
            if position['filter']!=fingerprint:raise ValueError()
            start,end=utc(position['start']),utc(position['end'])
        else:
            end=utc(end) if end else datetime.now(timezone.utc)
            start=utc(start) if start else end-timedelta(days=1)
        if start>=end or end-start>timedelta(days=366):raise ValueError()
        if ip:ip=str(ipaddress.IPv4Address(ip))
    except (ValueError,KeyError,TypeError):
        raise HTTPException(422,'Invalid IP, time range or cursor')
    params={'start':start,'end':end,'limit':limit+1}
    where=['source.timestamp >= {start:DateTime64(3)}','source.timestamp < {end:DateTime64(3)}']
    def known(field):
        return f'bitAnd(source.field_mask,{FIELD_BITS[field]}) != 0'
    if ip:
        where.append('source.router_ip={router:IPv4}');params['router']=ip
    if port is not None:
        if not 0<=port<=65535:raise HTTPException(422,'Invalid port')
        where.append('('+' OR '.join(f'({known(f)} AND source.{f}={{port:UInt16}})' for f in ('private_port','public_port','destination_port'))+')');params['port']=port
    terms=[term.strip() for term in keyword.split(',') if term.strip()]
    if len(terms)>10 or len(keyword)>512:raise HTTPException(422,'At most 10 terms / 512 characters')
    for i,term in enumerate(terms):
        key=f'term{i}'
        try:address=str(ipaddress.IPv4Address(term))
        except ValueError:address=None
        if address:
            checks=[f'({known(f)} AND source.{f}={{{key}:IPv4}})' for f in ('private_ip','public_ip','destination_ip')]
            checks.append(f'source.router_ip={{{key}:IPv4}}')
            where.append('('+' OR '.join(checks)+')');params[key]=address
        elif term.isdecimal() and 0<=int(term)<=65535:
            where.append('('+' OR '.join(f'({known(f)} AND source.{f}={{{key}:UInt16}})' for f in ('private_port','public_port','destination_port'))+f' OR source.subscriber_id={{{key}s:String}})')
            params[key],params[key+'s']=int(term),term
        else:
            where.append(f"positionCaseInsensitiveUTF8(concat(source.subscriber_id,' ',source.protocol,' ',source.application),{{{key}:String}})>0")
            params[key]=term
    client=get_client()
    total=client.query('SELECT count() FROM nat_sessions_v2 AS source WHERE '+' AND '.join(where),parameters=params,settings=QUERY_SETTINGS).result_rows[0][0] if include_total else None
    if position:
        try:params.update(before_time=utc(position['timestamp']),before_id=as_id(position['id']))
        except (ValueError,KeyError,TypeError):raise HTTPException(422,'Invalid cursor')
        where.append('(source.timestamp,source.record_id)<({before_time:DateTime64(3)},{before_id:UInt64})')
    select=[]
    for field in DISPLAY_FIELDS:
        if field in FIELD_BITS:
            expression=f'toString(source.{field})' if field.endswith('_ip') else f'source.{field}'
            select.append(f'if({known(field)},toNullable({expression}),NULL) AS {field}')
        elif field=='protocol':
            select.append("if(application='',upper(source.protocol),if(source.protocol='',application,concat(upper(source.protocol),' / ',application))) AS protocol")
        else:select.append('source.'+field)
    select.append('source.record_id')
    result=client.query('SELECT '+','.join(select)+' FROM nat_sessions_v2 AS source WHERE '+' AND '.join(where)+' ORDER BY source.timestamp DESC,source.record_id DESC LIMIT {limit:UInt32}',parameters=params,settings=QUERY_SETTINGS)
    rows=[dict(zip(result.column_names,row)) for row in result.result_rows]
    more=len(rows)>limit;rows=rows[:limit];next_cursor=None
    if more:
        last=rows[-1]
        next_cursor=base64.urlsafe_b64encode(json.dumps(dict(filter=fingerprint,start=start.isoformat(),end=end.isoformat(),timestamp=utc(last['timestamp']).isoformat(),id=str(last['record_id']))).encode()).decode()
    # Internal cursor identity and backend raw/diagnostic fields never reach UI/export.
    rows=[{field:row.get(field) for field in DISPLAY_FIELDS} for row in rows]
    return dict(results=rows,count=len(rows),total_count=total,next_cursor=next_cursor,start=start.isoformat(),end=end.isoformat(),query_ms=round((time.perf_counter()-started)*1000,2))
