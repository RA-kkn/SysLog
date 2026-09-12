"""Batch insert identity: distinct packets retain distinct IDs, even if identical."""
import hashlib
from parser import NAT_V2_COLUMNS


def insert_batch(client, rows, recover=False):
    pending = rows
    if recover and rows:
        existing = client.query(
            'SELECT record_id FROM nat_sessions_v2 WHERE record_id IN {ids:Array(UInt64)}',
            parameters={'ids': [int(row['record_id']) for row in rows]},
        ).result_rows
        seen = {str(item[0]) for item in existing}
        pending = [row for row in rows if str(row['record_id']) not in seen]
    if pending:
        token = hashlib.sha256('\n'.join(str(row['record_id']) for row in pending).encode()).hexdigest()
        client.insert('nat_sessions_v2', [[row[key] for key in NAT_V2_COLUMNS] for row in pending],
                      column_names=NAT_V2_COLUMNS,
                      settings={'insert_deduplicate': 1, 'insert_deduplication_token': token})
    return len(pending)
