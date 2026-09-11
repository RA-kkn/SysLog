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


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('destination')
    args = p.parse_args()
    print(json.dumps(snapshot(args.destination), indent=2))
