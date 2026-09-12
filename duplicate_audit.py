"""Read-only, bounded duplicate diagnostics. Never deduplicates or deletes rows."""
import json
from database import get_client


def report():
    client=get_client()
    results=[]
    for table in ('nat_sessions','nat_sessions_v2'):
        row=client.query(f'''SELECT count(),uniqExact(record_id),
            uniqExact(tuple(timestamp,router_ip,private_ip,private_port,public_ip,public_port,
                            destination_ip,destination_port,protocol,subscriber_id))
            FROM (SELECT timestamp,record_id,router_ip,private_ip,private_port,public_ip,public_port,
                         destination_ip,destination_port,protocol,subscriber_id
                  FROM {table} WHERE received_at>=now()-INTERVAL 1 HOUR
                  ORDER BY received_at DESC LIMIT 100000)''',
            settings={'max_execution_time':30,'max_memory_usage':500000000}).result_rows[0]
        results.append(dict(table=table,sampled_rows=row[0],repeated_record_ids=row[0]-row[1],
                            repeated_translation_timestamps=row[0]-row[2],sample_cap=100000))
    return dict(tables=results,note='Last hour, at most 100000 rows per table. Repeated record IDs indicate replay candidates; repeated endpoints/timestamps can be valid router packet logs. No deletion or deduplication performed.')


if __name__=='__main__':
    print(json.dumps(report(),indent=2))
