"""Read-only schema/size audit. Saves definitions, not a data backup."""
import json
from datetime import datetime, timezone
from pathlib import Path
import config
from database import get_client


def snapshot(destination):
    client = get_client()
    tables = client.query('SELECT name, create_table_query FROM system.tables WHERE database={db:String}',
                          parameters={'db': config.CLICKHOUSE_DB}).result_rows
    parts = client.query('SELECT table,sum(rows),sum(data_uncompressed_bytes),sum(data_compressed_bytes),sum(bytes_on_disk) FROM system.parts WHERE active AND database={db:String} GROUP BY table',
                         parameters={'db': config.CLICKHOUSE_DB}).result_rows
    report = {'at': datetime.now(timezone.utc).isoformat(), 'database': config.CLICKHOUSE_DB,
              'definitions': dict(tables), 'parts': [dict(zip(('table','rows','uncompressed_bytes','compressed_bytes','disk_bytes'),r)) for r in parts],
              'note': 'Schema backup only. No rows copied, rewritten, or deleted. Counts are active-part metadata.'}
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as out:
        json.dump(report, out, indent=2)
    return report


def validate(client):
    import re
    schema = (config.ROOT/'schema_structured.sql').read_text(encoding='utf-8')
    for table in ('nat_sessions', 'nat_sessions_v2', 'events'):
        definition = schema.split('CREATE TABLE IF NOT EXISTS syslog_db.'+table+'\n',1)[1].split('ENGINE',1)[0]
        expected = dict(re.findall(r"^\s*(\w+)\s+(.+?)\s+CODEC", definition, re.MULTILINE))
        actual = dict(client.query('SELECT name,type FROM system.columns WHERE database={db:String} AND table={table:String}',parameters={'db':config.CLICKHOUSE_DB,'table':table}).result_rows)
        mismatch = [name for name,typ in expected.items() if actual.get(name) != typ]
        if mismatch:
            raise RuntimeError(f'{table}: incompatible/missing column types: {mismatch}. Existing data was not rewritten. Review schema backup before starting services.')


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('destination')
    args = p.parse_args()
    print(json.dumps(snapshot(args.destination), indent=2))
