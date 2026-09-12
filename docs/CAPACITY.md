# Capacity work: target not yet certified

100,000 syslog packets/second and 100,000 dashboard HTTP requests/second are
different workloads. Internet subscriber count does not establish either rate.
Confirm average/peak log rate, router count, dashboard concurrency, and peak
duration before choosing a capacity profile. The existing VM has 16 vCPUs on an
Ivy Bridge VMware host; RAM, disk capacity/latency and network capacity still need
measurement. No VM or production configuration was changed by this audit.

At the local synthetic sample's 29.4466 compressed bytes/row:

| Sustained rate | Estimated compressed storage / 365 days |
|---|---:|
| 100,000 logs/sec | 92.86 TB |
| 4,307 logs/sec | Approximately 4 TB |

These exclude indexes, backup, replicas, merge headroom and spool. A short 100k
peak does not imply a 100k yearly average. `capacity.py` recalculates from supplied
measurements; it is not an ingestion benchmark.

Local parser-only test: 20,000 MikroTik records / 1.052 seconds, approximately
19,008 records/sec in one process. This excludes network, authorization, spool,
database and searches. It does not certify the older production CPU, and cannot
be multiplied by CPU count to promise capacity. The default listener uses one
worker; current end-to-end 100k throughput is unverified.

`deploy/capacity-audit.sh` reads CPU/RAM/disks, service status, socket limits and
UDP error counters without changing configuration or printing credentials.

The opt-in generator now supports real MikroTik-shaped packets and multiple UDP
source-port flows, with a duration bound. Its four-flow smoke test received and
parsed all 100 packets on an isolated local socket. This is not a load-capacity
result. Multiple flows on one source IP do not simulate multiple approved router
IPs. Existing device approval behavior is unchanged.

## Acceptance test on an isolated target with VM-equivalent resources

1. Use separate test DB, config DB, spool directory and UDP port. Never send
   synthetic load into the production listener.
2. Run a separate load-generator host so it does not consume collector CPU.
3. Test 5k, 10k, 25k, 50k and 100k logs/sec. Test both one hot flow and many flows;
   a good result for many flows does not establish one-router capacity. Linux
   networking uses flow-aware CPU distribution: see the
   [kernel scaling documentation](https://docs.kernel.org/networking/scaling.html).
4. Reconcile sent, received, parsed, spooled and inserted counts after drain.
   Check kernel UDP receive errors, queue/spool drops and worker imbalance.
   Generator send rate alone is not a pass.
5. Run concurrent bounded searches and exports; measure p95 latency and errors,
   insertion lag, CPU/RAM, disk latency and sustained spool growth.
6. Exercise database downtime, restart and spool replay. Confirm pending routers
   stay unauthorized and blocking one router leaves other routers ingesting.
7. Run a sustained soak across merges and representative traffic variability.
   Retention, storage budget and request latency targets must be agreed and met.

Do not tune away valid packet logs or blindly deduplicate to meet storage limits.
UDP has no delivery acknowledgment; a zero-loss guarantee is not established by
queue tuning or adding workers. Current spool retry semantics remain at-least-once
for persisted batches, with possible duplicate insertion after ambiguous replies.


## Storage-focused codec profile

The V2 creation schema now uses ZSTD(3), including Delta before ZSTD for timestamps.
The existing 20k-row local comparison measured 27.43785 bytes/row at level 3 and
27.1648 at level 9: about 1% extra saving at level 9. These samples do not certify
100k ingestion capacity or production compression. Existing table codecs are
unchanged by CREATE IF NOT EXISTS.

Generate a reviewable plan for existing V2 columns with:

```bash
.venv/bin/python manage.py compression-plan > compression-plan.sql
```

This only prints SQL. Before applying it with the existing service credentials,
save the schema with schema_audit.py and verify the profile against representative
traffic on the test target. The plan only changes CODEC; it does not change data
types, retention or sort keys, and does not force old parts to be rewritten.
ClickHouse documents CODEC changes under
[MODIFY COLUMN](https://clickhouse.com/docs/reference/statements/alter/column).

At level 3's sample size, 100k sustained stored logs/sec means 86.49 TB/year.
2 TB and 4 TB permit roughly 2,311 and 4,623 average logs/sec respectively before
headroom. A 4 TB budget at 100k sustained logs/sec would require 1.268 bytes/record;
the tested codec does not achieve this. Packet logging versus connection/mapping
logging must be explicitly agreed; the collector will not silently discard,
sample or deduplicate records to meet a storage budget.
