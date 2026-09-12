# Syslog Server / ISP Log Console

In-place upgrade of the existing UDP collector, ClickHouse storage, SQLite device approval, FastAPI and static web console.

**Current architecture: NAT-only.** Read [NAT_ONLY.md](NAT_ONLY.md) for additive deployment,
fallback/raw preservation, historical Events migration and packet-count verification.
It supersedes older All Logs/Events UI instructions and raw-free compression estimates.

## This Windows machine: run without Docker

Ubuntu/WSL and native ClickHouse 26.8.2.7 are now installed. From PowerShell in this directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-local.ps1
```

The launcher uses generated database credentials in the ignored `data/local-clickhouse.json`, asks you to create an admin password if needed, and starts the local website/listener. Open http://127.0.0.1:8000. This is local HTTP development, not a public HTTPS deployment. Keep `data/local-clickhouse.json` private. Database data is inside Ubuntu at `/var/lib/clickhouse`; Docker-volume backup commands do not apply to this installation.

Validated on this machine after setup: real ClickHouse connection, additive schema creation, empty All Logs and exact-IP NAT searches. Router ingestion/load and a live backup/restore still require validation.

- Manual router IP approval and automatic Pending discovery; only approved sources ingest.
- Expiring login sessions, ADMIN / OPERATOR / VIEWER roles, CSRF protection and user management.
- Bounded UDP queue, batched parsing, durable bounded spool and retrying ClickHouse writes.
- One production destination (`nat_sessions_v2`); unknown formats preserve original messages in hidden backend columns. Historical Events remain intact.
- Time-bounded IP/subscriber search, comma-separated AND terms, keyset pages and bounded CSV export.
- Branding, dark/light/system themes, system health and measured storage projections.

**Read [OPERATIONS.md](OPERATIONS.md) for exact startup, backup, restore, configuration, schema, test commands and remaining production gates.** Initial findings are in [AUDIT.md](AUDIT.md).

Actual router NAT formats and live ClickHouse throughput/compression have not yet been validated. The supplied database schema is additive; existing `syslogs` data and its original 90-day TTL remain unchanged. New tables target 365 days. A 4 TB/year footprint is conditional on measured traffic and compressed row size, never guaranteed.
