"""
T005: Phase 3 — 收斂與優化

執行步驟 (tw-quant-db-status.md §3.4 Phase 3):
1. 拆除 view 相容層 (core.v_*_stock, selector.v_*)
2. daily_prices range partition (monthly) + BRIN 索引
3. 權限收斂: 各 service account 僅具備所需 schema 權限
4. 備份與監控: pg_dump + 磁碟使用量告警

使用方式:
  DATABASE_URL=postgresql://twquant:pwd@localhost:5432/twquant_shared \
  python scripts/phase3_cleanup.py [--apply]

  --apply: 實際執行修改 (預設為 dry-run)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ─── Step 1: Drop compatibility views ──────────────────────────────────

# Views created by T001 (core) and T004 (selector) that can be dropped
# once selector code is fully migrated to use `symbol` directly.
VIEWS_TO_DROP = [
    "core.v_daily_prices_stock",
    "core.v_stocks_stock",
    "core.v_financials_stock",
    "core.v_monthly_revenue_stock",
    "selector.v_daily_prices",
    "selector.v_stocks",
    "selector.v_monthly_revenue",
    "selector.v_financials",
    "selector.v_valuations",
    "selector.v_signals",
]


async def drop_compat_views(conn, apply: bool = False):
    """拆除 view 相容層。僅在 selector 程式碼全面改用 symbol 後執行。"""
    logger.info("Step 1: Drop compatibility views")

    logger.warning(
        "  ⚠️  請確認 selector 程式碼已全面改用 symbol (grep 'stock_id' 应傳回 0)"
    )

    for view in VIEWS_TO_DROP:
        if apply:
            try:
                await conn.execute(f"DROP VIEW IF EXISTS {view}")
                logger.info("  ✓ Dropped %s", view)
            except (OSError, RuntimeError) as e:
                logger.warning("  ✗ Cannot drop %s: %s", view, e)
        else:
            logger.info("  [DRY RUN] Would drop %s", view)


# ─── Step 2: daily_prices range partition + BRIN index ────────────────

PARTITION_SQL = """
-- Convert core.daily_prices to monthly range partitioning (Phase 3)
-- Requires rebuilding the table as PARTITION BY RANGE
-- Step 2a: Create new partitioned table
DROP TABLE IF EXISTS core.daily_prices_new CASCADE;

CREATE TABLE core.daily_prices_new (
    symbol VARCHAR(10) NOT NULL,
    trade_date DATE NOT NULL,
    open NUMERIC(14,4),
    high NUMERIC(14,4),
    low NUMERIC(14,4),
    close NUMERIC(14,4),
    adjusted_close NUMERIC(14,4),
    volume BIGINT,
    turnover NUMERIC(20,2),

    source VARCHAR(100),
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    PRIMARY KEY(symbol, trade_date)
) PARTITION BY RANGE (trade_date);

-- Step 2b: Create monthly partitions for past 2 years + current
DO $$
DECLARE
    start_date DATE := DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '2 years';
    end_date DATE := DATE_TRUNC('month', CURRENT_DATE) + INTERVAL '2 years';
    current_date DATE := start_date;
    partition_name TEXT;
    next_month DATE;
BEGIN
    WHILE current_date < end_date LOOP
        next_month := current_date + INTERVAL '1 month';
        partition_name := 'daily_prices_' || TO_CHAR(current_date, 'YYYY_MM');

        EXECUTE format($sql$
            CREATE TABLE IF NOT EXISTS core.%I
            PARTITION OF core.daily_prices_new
            FOR VALUES FROM (%L) TO (%L)
        $sql$, partition_name, current_date, next_month);

        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_trade_date ON core.%I(trade_date)', partition_name, partition_name);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_symbol ON core.%I(symbol)', partition_name, partition_name);

        current_date := next_month;
    END LOOP;
END $$;

-- Step 2c: Copy data from old table to new partitioned table
INSERT INTO core.daily_prices_new SELECT * FROM core.daily_prices;

-- Step 2d: Drop old table and rename
DROP TABLE IF EXISTS core.daily_prices CASCADE;
ALTER TABLE core.daily_prices_new RENAME TO daily_prices;

-- Step 2e: Add BRIN index for large scans
CREATE INDEX IF NOT EXISTS idx_daily_prices_brin_date ON core.daily_prices USING BRIN(trade_date);
""".strip()


# ─── Step 3: Service account permissions ──────────────────────────────

PERMISSIONS_SQL = """
-- Service account roles (per spec §3.4)
-- Create roles first (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twquant_readonly') THEN
        CREATE ROLE twquant_readonly;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twquant_core_writer') THEN
        CREATE ROLE twquant_core_writer;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twquant_pickup') THEN
        CREATE ROLE twquant_pickup LOGIN PASSWORD 'twquant-secret-password';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twquant_selector') THEN
        CREATE ROLE twquant_selector LOGIN PASSWORD 'twquant-secret-password';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twquant_signal') THEN
        CREATE ROLE twquant_signal LOGIN PASSWORD 'twquant-secret-password';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twquant_audit_writer') THEN
        CREATE ROLE twquant_audit_writer;
    END IF;
END $$;

-- core 唯讀角色 (signal, selector, daybrain)
REVOKE ALL ON SCHEMA core FROM PUBLIC;
GRANT USAGE ON SCHEMA core TO twquant_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO twquant_readonly;

-- core 寫入角色 (pickup 攝取管線)
GRANT USAGE, CREATE ON SCHEMA core TO twquant_core_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA core TO twquant_core_writer;

-- pickup schema: pickup service 擁有全部
GRANT ALL ON SCHEMA pickup TO twquant_pickup;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA pickup TO twquant_pickup;

-- selector schema: selector service 擁有全部
GRANT ALL ON SCHEMA selector TO twquant_selector;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA selector TO twquant_selector;
GRANT twquant_readonly TO twquant_selector;

-- signal schema: signal service 擁有全部
GRANT ALL ON SCHEMA signal TO twquant_signal;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA signal TO twquant_signal;
GRANT twquant_readonly TO twquant_signal;

-- audit schema: 唯寫
GRANT USAGE ON SCHEMA audit TO twquant_audit_writer;
GRANT INSERT, SELECT ON ALL TABLES IN SCHEMA audit TO twquant_audit_writer;
""".strip()

# ─── Step 4: Backup script ────────────────────────────────────────────

BACKUP_SQL = """#!/bin/bash
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
pg_dump -h "$HOST" -U "$USER" -d "$DATABASE" --format=custom \\
  --blobs --no-owner --no-privileges \\
  -f "$BACKUP_DIR/twquant_full_$DATE.dump"

# Core 表增量備份 (僅當天新增的資料)
pg_dump -h "$HOST" -U "$USER" -d "$DATABASE" --format=custom \\
  --table='core.daily_prices' \\
  --table='core.market_context' \\
  --table='core.institutional_flow' \\
  --where="trade_date >= CURRENT_DATE - INTERVAL '1 day'" \\
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
"""


async def apply_partitions(conn, apply: bool = False):
    """建立 daily_prices 月度分區 + BRIN 索引。"""
    logger.info("Step 2: daily_prices range partition (monthly) + BRIN index")

    # Check data volume — skip partition if < 1M rows (per spec)
    row_count = await conn.fetchval("SELECT count(*) FROM core.daily_prices")
    if row_count < 1_000_000:
        logger.info("  ⏭️  Skip partitioning: core.daily_prices has %d rows (< 1M threshold)", row_count)
        logger.info("  Partitioning deferred until data volume exceeds 1M rows")
        return

    if apply:
        await conn.execute(PARTITION_SQL)
        logger.info("  ✓ Partitioned daily_prices with BRIN indexes")
    else:
        logger.info("  [DRY RUN] Would create monthly partitions + BRIN indexes")

async def apply_permissions(conn, apply: bool = False):
    """設定 service account 權限。"""
    logger.info("Step 3: Service account permissions")
    if apply:
        # Execute as single block (DO $$ blocks contain semicolons)
        await conn.execute(PERMISSIONS_SQL)
        logger.info("  ✓ Applied all service account permissions (6 roles + grants)")
    else:
        # Log each statement for dry-run
        for stmt in PERMISSIONS_SQL.split("\n"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                logger.info("  [DRY RUN] %s", stmt[:60])


def setup_backup_script():
    """建立備份腳本。"""
    logger.info("Step 4: Backup script")
    backup_path = Path(__file__).parent.parent / "scripts" / "pg_dump_twquant.sh"
    backup_path.write_text(BACKUP_SQL)
    backup_path.chmod(0o755)
    logger.info("  ✓ Created %s", backup_path)


async def main():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://localhost:5432/twquant_shared"
    )
    apply = "--apply" in sys.argv

    if not apply:
        logger.warning("DRY RUN mode — no changes will be applied")
        logger.warning("Use --apply to execute modifications")

    from asyncpg import connect

    conn = await connect(database_url)
    logger.info("Connected to PostgreSQL")

    await drop_compat_views(conn, apply)
    await apply_partitions(conn, apply)
    await apply_permissions(conn, apply)
    setup_backup_script()

    if apply:
        await conn.execute(
            "ALTER DATABASE twquant_shared SET search_path TO public, core"
        )
        logger.info("  ✓ Set default search_path")

    await conn.close()
    logger.info("Phase 3 complete")


if __name__ == "__main__":
    asyncio.run(main())
