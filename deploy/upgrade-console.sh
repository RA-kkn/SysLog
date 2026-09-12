#!/usr/bin/env bash
# Existing native systemd deployment only. No Docker, data deletes or TTL changes.
set -euo pipefail
cd /opt/SysLog
test -x .venv/bin/python
test -f /etc/syslog-console.env
stamp=$(date -u +%Y%m%dT%H%M%SZ)

# Use the same protected environment/user as the running services. Do not source
# the environment file as shell code or expose its password in command arguments.
run_admin() {
    systemd-run --quiet --wait --pipe --collect \
        --property=User=syslog-console --property=Group=syslog-console \
        --property=EnvironmentFile=/etc/syslog-console.env \
        --working-directory=/opt/SysLog \
        /opt/SysLog/.venv/bin/python "$@"
}
run_admin manage.py backup-config "data/backups/config-${stamp}.db"
run_admin manage.py prepare-nat-v2 "data/backups/schema-${stamp}.json"
run_admin manage.py compression-plan --apply --schema-backup "data/backups/codecs-${stamp}.json"
run_admin manage.py set-display-timezone Asia/Karachi
systemctl restart syslog-console-listener syslog-console-api
systemctl is-active syslog-console-listener syslog-console-api
curl --fail --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/health
echo
echo 'Upgrade complete. Existing tables, retention, credentials and spool preserved.'
