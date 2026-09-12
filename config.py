"""Environment configuration shared by listener and API."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def integer(name, default, minimum=1):
    value = int(os.getenv(name, default))
    if value < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    return value

LISTEN_HOST = os.getenv('LISTEN_HOST', '0.0.0.0')
LISTEN_PORT = integer('LISTEN_PORT', 514)
NUM_WORKERS = integer('NUM_WORKERS', 1)
BATCH_MAX_ROWS = integer('BATCH_MAX_ROWS', 2000)
BATCH_MAX_SECONDS = float(os.getenv('BATCH_MAX_SECONDS', '1'))
if BATCH_MAX_SECONDS <= 0:
    raise ValueError('BATCH_MAX_SECONDS must be positive')
QUEUE_SIZE = integer('QUEUE_SIZE', 50000)
SPOOL_MAX_BYTES = integer('SPOOL_MAX_BYTES', 1024 * 1024 * 1024)
SOCKET_RCVBUF = integer('SOCKET_RCVBUF', 8 * 1024 * 1024)
DATA_DIR = Path(os.getenv('DATA_DIR', str(ROOT / 'data'))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.getenv('CONFIG_DB_PATH', str(ROOT / 'devices.db'))
CLICKHOUSE_HOST = os.getenv('CLICKHOUSE_HOST', 'localhost')
CLICKHOUSE_PORT = integer('CLICKHOUSE_PORT', 8123)
CLICKHOUSE_USER = os.getenv('CLICKHOUSE_USER', 'default')
CLICKHOUSE_PASSWORD = os.getenv('CLICKHOUSE_PASSWORD', '')
CLICKHOUSE_DB = os.getenv('CLICKHOUSE_DB', 'syslog_db')
if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', CLICKHOUSE_DB):
    raise ValueError('Invalid CLICKHOUSE_DB identifier')
CLICKHOUSE_SECURE = os.getenv('CLICKHOUSE_SECURE', 'false').lower() == 'true'
CLICKHOUSE_TABLE = 'syslogs'
RETENTION_DAYS = integer('RETENTION_DAYS', 365)
EXPORT_LIMIT = integer('EXPORT_LIMIT', 50000)
SESSION_SECONDS = integer('SESSION_SECONDS', 28800)
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'true').lower() == 'true'
STORAGE_BUDGET_BYTES = integer('STORAGE_BUDGET_BYTES', 4_000_000_000_000)
DISPLAY_TIMEZONE = os.getenv('DISPLAY_TIMEZONE', 'Asia/Karachi')
