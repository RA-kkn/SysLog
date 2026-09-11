#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg
curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key -o /tmp/clickhouse-key.asc
gpg --batch --yes --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg /tmp/clickhouse-key.asc
arch=$(dpkg --print-architecture)
echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=$arch] https://packages.clickhouse.com/deb stable main" > /etc/apt/sources.list.d/clickhouse.list
apt-get update -qq
apt-get install -y -qq clickhouse-server clickhouse-client
install -m 600 -o clickhouse -g clickhouse "$1" /etc/clickhouse-server/users.d/syslog-local.xml
cat > /etc/clickhouse-server/config.d/local-listen.xml <<'EOF'
<clickhouse><listen_host replace="replace">127.0.0.1</listen_host></clickhouse>
EOF
service clickhouse-server restart
