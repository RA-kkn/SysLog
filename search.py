"""Bounded server-side queries with a stable time window and keyset cursor."""
import base64
import hashlib
import ipaddress
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from database import get_client

LIMITS = (100, 200, 500, 1000, 5000)
def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

def query_logs(kind='all', keyword='', ip='', start=None, end=None, limit=200, cursor=None,
               port=None, severity=None, hostname='', event_type=''):
    if kind not in ('all', 'nat_sessions', 'events', 'legacy') or limit not in LIMITS:
        raise HTTPException(422, 'Invalid category or row limit')
    fingerprint = hashlib.sha256(json.dumps([kind,keyword,ip,port,severity,hostname,event_type]).encode()).hexdigest()
    position = None
    try:
        if cursor:
            if len(cursor) > 2048:
                raise ValueError()
            position = json.loads(base64.urlsafe_b64decode(cursor))
            if position['filter'] != fingerprint:
                raise ValueError()
            start, end = utc(position['start']), utc(position['end'])
        else:
            end = utc(end) if end else datetime.now(timezone.utc)
            start = utc(start) if start else end - timedelta(days=1)
        if start >= end or end - start > timedelta(days=366):
            raise ValueError()
        if ip:
            ip = str(ipaddress.IPv4Address(ip))
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, 'Invalid IP, time range, or cursor (maximum range: 366 days)')
    params = {'start': start, 'end': end, 'limit': limit + 1}
    if kind == 'legacy':
        if cursor:
            raise HTTPException(422,'Legacy records have no unique cursor key; narrow the time range')
        where=['received_at >= {start:DateTime64(3)}','received_at < {end:DateTime64(3)}']
        if ip:
            where.append('device_ip={router:IPv4}');params['router']=ip
        for i,term in enumerate(t.strip() for t in keyword.split(',') if t.strip()):
            where.append(f'positionCaseInsensitiveUTF8(message, {{term{i}:String}})>0');params[f'term{i}']=term
        if port is not None:
            where.append('source_port={port:UInt16}');params['port']=port
        if severity is not None:
            where.append('severity={severity:UInt8}');params['severity']=severity
        if hostname:
            where.append('hostname={hostname:String}');params['hostname']=hostname
        if event_type:
            where.append('process_name={event_type:String}');params['event_type']=event_type
        result=get_client().query("SELECT received_at AS timestamp,received_at,toString(device_ip) AS router_ip, 'legacy' AS kind, process_name AS event_type, message FROM syslogs WHERE "+' AND '.join(where)+' ORDER BY received_at DESC LIMIT {limit:UInt32}',parameters=params,settings={'max_execution_time':30,'max_memory_usage':1_000_000_000})
        rows=[dict(zip(result.column_names,row)) for row in result.result_rows]
        return dict(results=rows[:limit],count=min(len(rows),limit),next_cursor=None,start=start.isoformat(),end=end.isoformat(),
                    truncated=len(rows)>limit,notice='Legacy table: bounded results only; narrow time range for more records. Existing TTL is unchanged.')
    subqueries = []
    for table in ('nat_sessions', 'events') if kind == 'all' else (kind,):
        nat = table == 'nat_sessions'
        where = ['timestamp >= {start:DateTime64(3)}', 'timestamp < {end:DateTime64(3)}',
                 'toDate(timestamp) >= toDate({start:DateTime64(3)})', 'toDate(timestamp) <= toDate({end:DateTime64(3)})']
        if ip:
            where.append('router_ip = {router:IPv4}')
            params['router'] = ip
        if port is not None:
            fields = ('private_port','public_port','destination_port') if nat else ('source_port',)
            where.append('(' + ' OR '.join(f'{f} = {{port:UInt16}}' for f in fields) + ')')
            params['port'] = port
        if severity is not None:
            where.append('0' if nat else 'severity = {severity:UInt8}')
            params['severity'] = severity
        if hostname:
            where.append('0' if nat else 'hostname = {hostname:String}')
            params['hostname'] = hostname
        if event_type:
            where.append('0' if nat else 'event_type = {event_type:String}')
            params['event_type'] = event_type
        terms = [t.strip() for t in keyword.split(',') if t.strip()]
        if len(terms) > 10 or len(keyword) > 512:
            raise HTTPException(422, 'At most 10 search terms / 512 characters')
        for i, term in enumerate(terms):
            key = f'term{i}'
            try:
                address = str(ipaddress.IPv4Address(term))
            except ValueError:
                address = None
            if nat and address:
                where.append('(' + ' OR '.join(f'{field} = {{{key}:IPv4}}' for field in ('private_ip','public_ip','destination_ip','router_ip')) + ')')
                params[key] = address
            elif nat and term.isdecimal() and 0 <= int(term) <= 65535:
                where.append('(' + ' OR '.join(f'{field} = {{{key}:UInt16}}' for field in ('private_port','public_port','destination_port')) + f' OR subscriber_id = {{{key}s:String}})')
                params[key], params[key+'s'] = int(term), term
            elif address:
                # Token boundaries avoid matching 10.0.0.1 inside 10.0.0.10.
                where.append(f'(router_ip = {{{key}:IPv4}} OR match(message, {{{key}regex:String}}))')
                params[key] = address
                params[key+'regex'] = r'(^|[^0-9.])' + address.replace('.', r'\.') + r'([^0-9.]|$)'
            else:
                expression = "concat(subscriber_id, ' ', protocol)" if nat else 'message'
                where.append(f'positionCaseInsensitiveUTF8({expression}, {{{key}:String}}) > 0')
                params[key] = term
        if position:
            try:
                params['before_time'] = utc(position['timestamp'])
                params['before_id'] = uuid.UUID(position['id'])
                if position['kind'] not in ('events','nat_sessions'):
                    raise ValueError()
                params['before_kind'] = position['kind']
            except (ValueError, KeyError, TypeError):
                raise HTTPException(422, 'Invalid cursor')
            where.append(f"(timestamp, record_id, '{table}') < ({{before_time:DateTime64(3)}}, {{before_id:UUID}}, {{before_kind:String}})")
        common = f"timestamp, received_at, record_id, '{table}' AS kind, toString(router_ip) AS router_ip"
        if nat:
            selection = common + ", toString(private_ip) AS private_ip, private_port, toString(public_ip) AS public_ip, public_port, toString(destination_ip) AS destination_ip, destination_port, protocol, subscriber_id, '' AS hostname, '' AS event_type, CAST(NULL AS Nullable(UInt8)) AS severity, '' AS message"
        else:
            selection = common + ", '' AS private_ip, toUInt16(0) AS private_port, '' AS public_ip, toUInt16(0) AS public_port, '' AS destination_ip, toUInt16(0) AS destination_port, '' AS protocol, '' AS subscriber_id, hostname, event_type, toNullable(severity) AS severity, message"
        # Qualify native IP predicates so ClickHouse cannot substitute the
        # display toString(...) aliases into an IPv4 equality predicate.
        predicate = re.sub(r'\b(router_ip|private_ip|public_ip|destination_ip)\b', r'source.\1', ' AND '.join(where))
        subqueries.append(f"SELECT {selection} FROM {table} AS source WHERE {predicate}")
    sql = 'SELECT * FROM (' + ' UNION ALL '.join(subqueries) + ') ORDER BY timestamp DESC, record_id DESC, kind DESC LIMIT {limit:UInt32}'
    # SELECT * is over an explicit, filtered projection, never a base table.
    result = get_client().query(sql, parameters=params, settings={'max_execution_time':30, 'max_memory_usage':1_000_000_000})
    rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        last = rows[-1]
        payload = dict(filter=fingerprint, start=start.isoformat(), end=end.isoformat(),
                       timestamp=utc(last['timestamp']).isoformat(), id=str(last['record_id']), kind=last['kind'])
        next_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return dict(results=rows, count=len(rows), next_cursor=next_cursor, start=start.isoformat(), end=end.isoformat())
