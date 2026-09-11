"""Explicit administration; no automatic production migrations or stress tests."""
import argparse
import getpass
import json
import sqlite3
from pathlib import Path
from contextlib import closing
import config

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command',required=True)
    admin = sub.add_parser('create-admin'); admin.add_argument('username')
    sub.add_parser('init-schema')
    ttl = sub.add_parser('retention-plan'); ttl.add_argument('--days',type=int,default=config.RETENTION_DAYS)
    sub.add_parser('storage')
    backup = sub.add_parser('backup-config'); backup.add_argument('destination')
    args = parser.parse_args()
    if args.command == 'create-admin':
        import auth
        import device_store
        with device_store._conn() as c:
            if c.execute('SELECT 1 FROM users WHERE username=?',(args.username,)).fetchone():
                parser.error('User exists; use console to reset password')
        password = getpass.getpass('New admin password (12+ characters): ')
        if password != getpass.getpass('Repeat password: '):
            parser.error('Passwords do not match')
        auth.save_user(args.username,'',password,'ADMIN',True,True)
        print('Administrator created')
    elif args.command == 'init-schema':
        # Connect to default first: target database may not exist on fresh installs.
        import clickhouse_connect
        client = clickhouse_connect.get_client(host=config.CLICKHOUSE_HOST,port=config.CLICKHOUSE_PORT,
            username=config.CLICKHOUSE_USER,password=config.CLICKHOUSE_PASSWORD,secure=config.CLICKHOUSE_SECURE)
        sql = (config.ROOT/'schema_structured.sql').read_text().replace('syslog_db',config.CLICKHOUSE_DB)
        sql = sql.replace('INTERVAL 365 DAY',f'INTERVAL {config.RETENTION_DAYS} DAY')
        sql = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
        for statement in sql.split(';'):
            if statement.strip():
                client.command(statement)
        print('Additive schema applied. Existing tables/TTLs were not modified.')
    elif args.command == 'retention-plan':
        if not 1 <= args.days <= 3650:
            parser.error('Days must be 1..3650')
        print('-- REVIEW ONLY: applying this SQL allows deletion of records older than the new TTL.')
        print('-- Back up and confirm the retention requirement first. No SQL has been executed.')
        for table in ('nat_sessions','events'):
            print(f'ALTER TABLE {config.CLICKHOUSE_DB}.{table} MODIFY TTL toDateTime(timestamp) + INTERVAL {args.days} DAY DELETE;')
    elif args.command == 'storage':
        from monitoring import report
        print(json.dumps(report(),indent=2,default=str))
    elif args.command == 'backup-config':
        destination = Path(args.destination)
        if destination.exists():
            parser.error('Destination already exists')
        destination.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(config.DB_PATH)) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        print(f'Consistent SQLite backup: {destination}')

if __name__ == '__main__':
    main()
