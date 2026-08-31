#!/bin/bash
# 每日 PostgreSQL 備份腳本
# cron: 0 2 * * * pg_dump_twquant.sh >> /var/log/twquant-backup.log 2>&1

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups/twquant}"
DATE=$(date +%Y%m%d_%H%M%S)
DATABASE="${DATABASE:-twquant_shared}"
HOST="${DB_HOST:-localhost}"
USER="${DB_USER:-twquant}"
PASSWORD="${DB_PASSWORD:-twquant-secret-password}"
export PGPASSWORD="$PASSWORD"

mkdir -p "$BACKUP_DIR"

# 完整備份
pg_dump -h "$HOST" -U "$USER" -d "$DATABASE" --format=custom \
  --blobs --no-owner --no-privileges \
  -f "$BACKUP_DIR/twquant_full_$DATE.dump"

# Core 表增量備份 (僅當天新增的資料)
pg_dump -h "$HOST" -U "$USER" -d "$DATABASE" --format=custom \
  --table='core.daily_prices' \
  --table='core.market_context' \
  --table='core.institutional_flow' \
  --where="trade_date >= CURRENT_DATE - INTERVAL '1 day'" \
  -f "$BACKUP_DIR/twquant_core_incremental_$DATE.dump"

# 驗證備份
pg_restore --list "$BACKUP_DIR/twquant_full_$DATE.dump" > /dev/null 2>&1
echo "[$(date)] Backup completed: $BACKUP_DIR/twquant_full_$DATE.dump"

# 清理 7 天前備份
find "$BACKUP_DIR" -name "twquant_*.dump" -mtime +7 -delete

# 磁碟使用量告警 (>80% 通知)
DISK_USAGE=$(df "$BACKUP_DIR" | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_USAGE" -gt 80 ]; then
  echo "[$(date)] WARNING: 備份磁碟使用率 ${DISK_USAGE}% > 80%" | tee -a "$BACKUP_DIR/backup_alerts.log"
fi
