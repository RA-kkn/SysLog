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
    sub.add_parser('wipe-logs')
    compact = sub.add_parser('migrate-record-id')
    compact.add_argument('--schema-backup', required=True)
    migration = sub.add_parser('prepare-nat-v2'); migration.add_argument('schema_backup')
    ttl = sub.add_parser('retention-plan'); ttl.add_argument('--days',type=int,default=config.RETENTION_DAYS)
    sub.add_parser('storage')
    compression = sub.add_parser('compression-plan')
    compression.add_argument('--apply',action='store_true')
    compression.add_argument('--schema-backup')
    tz = sub.add_parser('set-display-timezone'); tz.add_argument('timezone')
    backup = sub.add_parser('backup-config'); backup.add_argument('destination')
    args = parser.parse_args()
    if args.command in ('wipe-logs', 'migrate-record-id'):
        from database import get_client
        from log_admin import wipe, migrate_id_schema
        if args.command == 'wipe-logs':
            print('PERMANENT: clears only known ClickHouse log tables. Stop all writers and drain spool first.')
            confirmation = input('Type DELETE ALL LOG DATA: ')
            wipe(get_client(), confirmation)
        else:
            confirmation = input('Type MIGRATE EMPTY NAT TABLE: ')
            migrate_id_schema(get_client(), confirmation, args.schema_backup)
    elif args.command == 'create-admin':
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
    elif args.command in ('init-schema', 'prepare-nat-v2'):
        if args.command == 'prepare-nat-v2':
            from schema_audit import snapshot
            snapshot(args.schema_backup)
        # Connect to default first: target database may not exist on fresh installs.
        import clickhouse_connect
        client = clickhouse_connect.get_client(host=config.CLICKHOUSE_HOST,port=config.CLICKHOUSE_PORT,
            username=config.CLICKHOUSE_USER,password=config.CLICKHOUSE_PASSWORD,secure=config.CLICKHOUSE_SECURE)
        sql = (config.ROOT/'schema_structured.sql').read_text(encoding='utf-8').replace('syslog_db',config.CLICKHOUSE_DB)
        sql = sql.replace('INTERVAL 365 DAY',f'INTERVAL {config.RETENTION_DAYS} DAY')
        sql = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
        for statement in sql.split(';'):
            if statement.strip():
                client.command(statement)
        from schema_audit import validate
        validate(client)
        print('Additive schema applied. Existing tables/TTLs were not modified.')
    elif args.command == 'compression-plan':
        from compression import plan, apply
        if args.apply:
            if not args.schema_backup:
                parser.error('--apply requires --schema-backup (a new destination)')
            apply(args.schema_backup)
            print('NAT ZSTD(9) codecs applied and verified. Schema backed up; no forced data rewrite.')
        else:
            print('-- REVIEW ONLY. No database command executed.')
            print('-- CODEC only; no type, TTL, sort key or data deletion changes.')
            print('-- Existing parts are not forcibly rewritten. Monitor ingestion and merge CPU.')
            print(plan())
    elif args.command == 'retention-plan':
        if not 1 <= args.days <= 3650:
            parser.error('Days must be 1..3650')
        print('-- REVIEW ONLY: applying this SQL allows deletion of records older than the new TTL.')
        print('-- Back up and confirm the retention requirement first. No SQL has been executed.')
        for table in ('nat_sessions','nat_sessions_v2','events'):
            print(f'ALTER TABLE {config.CLICKHOUSE_DB}.{table} MODIFY TTL toDateTime(timestamp) + INTERVAL {args.days} DAY DELETE;')
    elif args.command == 'set-display-timezone':
        from zoneinfo import ZoneInfo
        import device_store
        ZoneInfo(args.timezone)
        device_store.set_setting('display_timezone', args.timezone)
        print('Display timezone saved; UTC database timestamps unchanged.')
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
