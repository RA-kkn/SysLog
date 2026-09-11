import threading
import clickhouse_connect
import config

_local = threading.local()
def get_client():
    if not getattr(_local, 'client', None):
        _local.client = clickhouse_connect.get_client(
            host=config.CLICKHOUSE_HOST, port=config.CLICKHOUSE_PORT,
            username=config.CLICKHOUSE_USER, password=config.CLICKHOUSE_PASSWORD,
            database=config.CLICKHOUSE_DB, secure=config.CLICKHOUSE_SECURE,
            autogenerate_session_id=False, connect_timeout=5, send_receive_timeout=30,
            settings={'session_timezone':'UTC'})
    return _local.client

def reset_client():
    client = getattr(_local, 'client', None)
    if client:
        try:
            client.close()
        except Exception:
            pass
    _local.client = None
