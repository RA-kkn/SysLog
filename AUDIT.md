# Existing application audit

Inspected every supplied Python, SQL, deployment, HTML, SVG and configuration file on 2026-09-11. Source snapshot is in `backups/source-*.zip`. No running deployment or production database was supplied; Docker daemon is unavailable locally.

## Reusable architecture

Python UDP collector, RFC3164/RFC5424 parser, ClickHouse HTTP client, SQLite device/settings store, FastAPI and a dependency-free HTML console. Preserve these components and existing device approvals. No firewall or FreeRADIUS integration exists.

## Findings before changes

- Critical: no authentication or backend authorization on any administrative endpoint.
- Critical: insert exceptions discard batches; two threads share a ClickHouse session; receive thread can perform inserts.
- Critical: untrusted messages/names rendered with innerHTML; unrestricted image uploads trust MIME and extension.
- High: no bounded queue, durable retry, shutdown drain or ingest metrics.
- High: no NAT extraction; duplicate message/raw_message for every row.
- High: search has no default time window or cursor; weak limit validation.
- High: original syslogs table has 90-day TTL and daily partitions. Changing the schema file does not migrate existing installations.
- High: Docker publishes unauthenticated database interfaces on all addresses; mutable latest image.
- Medium: denied packets perform synchronous SQLite writes; device cache is eventually consistent (five seconds).
- Missing: users, sessions, CSV, storage/health monitoring, tests and recovery procedure.

## Rollout constraints

New tables are additive. Existing syslogs data and its TTL must be reviewed independently before rollout; do not silently migrate or alter retention. Structured NAT extraction must be conservative: unknown or incomplete formats stay in Events with original text. Actual router samples were not supplied, so synthetic fixtures cannot certify vendor coverage. Benchmarks must be explicit and isolated; no production load generation or firewall changes.
