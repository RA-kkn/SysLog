#!/usr/bin/env bash
# Read-only VM inventory. Does not print passwords or change sysctl/firewall.
set -euo pipefail
printf '\nCPU\n'
lscpu
printf '\nMemory\n'
free -h
printf '\nDisk capacity\n'
df -h / /var/lib/clickhouse /opt/SysLog
printf '\nBlock devices\n'
lsblk -o NAME,TYPE,SIZE,ROTA,MOUNTPOINTS
printf '\nServices\n'
systemctl is-active clickhouse-server syslog-console-api syslog-console-listener nginx || true
printf '\nUDP counters (cumulative since boot)\n'
awk '/^Udp:/{print}' /proc/net/snmp
printf '\nSocket buffer limits\n'
sysctl net.core.rmem_default net.core.rmem_max net.core.netdev_max_backlog
printf '\nRun this script again after an isolated load test to compare UDP error counters.\n'
