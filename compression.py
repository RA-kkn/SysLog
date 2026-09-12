"""Apply the tested NAT codec profile, after a mandatory schema snapshot."""
import re
import config
from database import get_client
from schema_audit import snapshot, validate


def fields():
    sql=(config.ROOT/'schema_structured.sql').read_text(encoding='utf-8')
    definition=sql.split('CREATE TABLE IF NOT EXISTS syslog_db.nat_sessions_v2',1)[1].split('ENGINE',1)[0]
    result=re.findall(r'^\s*(\w+)\s+.+?\s+CODEC\((.+)\),?$',definition,re.MULTILINE)
    if len(result)!=20 or any('ZSTD(9)' not in codec for _,codec in result):
        raise RuntimeError('Unexpected NAT codec schema; nothing applied')
    return result


def plan():
    return (f'ALTER TABLE {config.CLICKHOUSE_DB}.nat_sessions_v2\n' +
            ',\n'.join(f'  MODIFY COLUMN {name} CODEC({codec})' for name,codec in fields())+';')


def apply(backup_path):
    statement=plan()
    client=get_client()
    validate(client)
    snapshot(backup_path)  # Exclusive file creation; any failure prevents ALTER.
    client.command(statement)
    codecs=dict(client.query('SELECT name,compression_codec FROM system.columns '
        'WHERE database={db:String} AND table={table:String}',
        parameters={'db':config.CLICKHOUSE_DB,'table':'nat_sessions_v2'}).result_rows)
    for name,codec in fields():
        expression=codecs.get(name,'').upper().replace(' ','')
        if 'ZSTD(9)' not in expression or ('Delta' in codec and 'DELTA' not in expression):
            raise RuntimeError(f'Codec verification failed for {name}; inspect saved schema and server')
