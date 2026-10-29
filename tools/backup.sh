#!/usr/bin/env bash
# 每日备份：n8n 库 + cf 业务库 + worker 密钥，保留 7 天。由 /etc/cron.d/cf-n8n 每天 01:00 调用。
set -euo pipefail
cd "$(dirname "$0")/.."
if docker info >/dev/null 2>&1; then DOCKER=docker; else DOCKER="sudo docker"; fi
set -a; . ./.env; set +a
D=backups/$(date +%Y%m%d)
mkdir -p "$D"
for db in "$POSTGRES_DB" cf; do
  $DOCKER compose exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc "$db" > "$D/$db.dump"
done
tar -czf "$D/secrets.tgz" -C /data cf-n8n-secrets 2>/dev/null || true
chmod -R go-rwx "$D"
find backups -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
echo "$(date '+%F %T') backup ok -> $D"
