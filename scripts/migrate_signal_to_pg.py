"""
T009: tw-quant-signal SQLite → PostgreSQL 移植腳本

將 signal 本地 SQLite 資料庫匯出到共享 PostgreSQL 的 signal schema。

使用方式:
  DATABASE_URL=postgresql://twquant:pwd@localhost:5432/twquant_shared \\
  SIGNAL_SQLITE_DB=/path/to/signal.db \\
  python scripts/migrate_signal_to_pg.py

原則:
- 逐表匯出: sqlite3 → pandas → psycopg2 executemany INSERT ON CONFLICT DO NOTHING
- stock_id 保持不變 (signal schema 使用 stock_id 而非 core 的 symbol)
- daily_prices/dividends/institutional_flows 是唯讀 VIEW (mapping core.*)，會跳過
- 支援 --dry-run (僅顯示計畫不執行)
- 支援 --table TABLE (僅遷移指定表格)
- 使用 psycopg2 (同步) 而非 asyncpg，避免 int32 overflow 和 numeric precision 問題
"""

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import psycopg2
from asyncpg import connect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# All tables in signal schema (from signal/schema.sql)
ALL_TABLES = [
    "pipeline_log",
    "daily_prices",
    "market_index",
    "institutional_flows",
    "signals",
    "rule_signals",
    "features",
    "tech_indicators",
    "financial_data",
    "margin_data",
    "quarterly_financials",
    "dividends",
    "margin_trading",
    "risk_metrics",
    "health_scores",
    "weekly_indicators",
    "monthly_health_scores",
    "weekly_health_scores",
    "multi_timeframe_consensus",
    "monthly_indicators",
    "structural_drift",
    "operation_log",
    "monthly_revenue",
    "scorecard",
    "performance_log",
    "watchlist_history",
]


def get_sqlite_tables(db_path: str) -> list[str]:
    """取得 SQLite 資料庫中的所有表名。"""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    return tables


def parse_database_url(database_url: str) -> dict:
    """Parse DATABASE_URL into psycopg2 connection params."""
    parsed = urlparse(database_url)
    return {
        'host': parsed.hostname or 'localhost',
        'port': parsed.port or 5432,
        'user': parsed.username or 'twquant',
        'password': parsed.password or 'twquant-secret-password',
        'dbname': parsed.path[1:] or 'twquant_shared',
    }


async def migrate_table(
    pg_conn, sqlite_db: str, table: str, dry_run: bool = False,
    database_url: str = None,
) -> int:
    """將單一表從 SQLite 匯出到 PostgreSQL。

    使用 pandas + psycopg2 executemany 進行批次插入。
    INSERT ON CONFLICT DO NOTHING — 支援中斷續跑。
    """
    # 讀取 SQLite 資料
    sqlite_conn = sqlite3.connect(sqlite_db)
    df = pd.read_sql_query(f"SELECT * FROM {table}", sqlite_conn)
    sqlite_conn.close()

    if len(df) == 0:
        logger.info("  %s: 0 rows, skipping", table)
        return 0

    # Skip views (daily_prices, dividends, institutional_flows are read-only views mapping core.*)
    is_view = await pg_conn.fetchval(
        "SELECT 'v' FROM pg_views WHERE schemaname = 'signal' AND viewname = $1",
        table,
    )
    if is_view == 'v':
        logger.info("  %s: is a read-only view, skipping (data via core.* backfill)", table)
        return 0

    # 取得 PostgreSQL 目標表的欄位 (驗證相容性)
    pg_cols = await pg_conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'signal' AND table_name = $1 "
        "ORDER BY ordinal_position",
        table,
    )
    pg_col_names = [row["column_name"] for row in pg_cols]

    if not pg_col_names:
        logger.warning("  %s: PG signal schema 中找不到，跳過", table)
        return 0

    # 確認 SQLite 欄位在 PG 表中存在
    sqlite_cols = [c for c in df.columns if c in pg_col_names]
    missing = set(df.columns) - set(pg_col_names)
    if missing:
        logger.warning("  %s: 欄位缺失 — %s", table, missing)

    if not sqlite_cols:
        logger.warning("  %s: 無可對應欄位，跳過", table)
        return 0

    # 過濾 dataframe 欄位
    df = df[sqlite_cols]

    if dry_run:
        logger.info("  %s: DRY RUN — %d rows would be migrated", table, len(df))
        return len(df)

    # 批次插入 (INSERT ON CONFLICT DO NOTHING — 支援中斷續跑)
    col_str = ", ".join(f'"{c}"' for c in sqlite_cols)
    placeholders = ", ".join(["%s"] * len(sqlite_cols))
    pg_params = parse_database_url(database_url)

    count = 0
    for batch_start in range(0, len(df), 10000):
        batch = df.iloc[batch_start : batch_start + 10000]

        # Convert to Python native types (handles numpy → Python, NaN → None)
        records = []
        for _, row in batch.iterrows():
            record = []
            for col in sqlite_cols:
                val = row[col]
                if val != val:  # NaN → None
                    record.append(None)
                elif hasattr(val, 'item'):  # numpy scalar → Python
                    record.append(val.item())
                else:
                    record.append(val)
            records.append(tuple(record))

        # Use psycopg2 for bulk insert (avoids asyncpg int32 overflow and numeric precision issues)
        with psycopg2.connect(**pg_params) as pg_sync:
            with pg_sync.cursor() as cur:
                cur.executemany(
                    f'INSERT INTO signal."{table}" ({col_str}) '
                    f'VALUES ({placeholders}) '
                    f'ON CONFLICT DO NOTHING',
                    records,
                )
                pg_sync.commit()

        batch_count = len(records)
        count += batch_count
        logger.info("  %s: batch migrated %d rows (total: %d)", table, batch_count, count)

    return count


async def main():
    sqlite_db = os.environ.get("SIGNAL_SQLITE_DB", "")
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://localhost:5432/twquant_shared"
    )
    dry_run = "--dry-run" in sys.argv
    only_table = None
    if "--table" in sys.argv:
        idx = sys.argv.index("--table")
        only_table = sys.argv[idx + 1]

    if not sqlite_db or not Path(sqlite_db).exists():
        logger.error("請設定 SIGNAL_SQLITE_DB 環境變數指向 signal SQLite 檔案")
        sys.exit(1)

    # 建立 PostgreSQL 連線
    conn = await connect(database_url)
    await conn.execute("SET search_path TO signal, pickup, core, public")
    logger.info("Connected to PostgreSQL: %s", database_url)

    # 取得 SQLite 表列表
    sqlite_tables = get_sqlite_tables(sqlite_db)
    tables_to_migrate = [t for t in ALL_TABLES if t in sqlite_tables]
    if only_table:
        tables_to_migrate = [t for t in tables_to_migrate if t == only_table]

    if not tables_to_migrate:
        logger.info("No tables to migrate")
        return

    logger.info("Tables to migrate: %s", tables_to_migrate)

    # 逐表遷移
    total_rows = 0
    for table in tables_to_migrate:
        total_rows += await migrate_table(conn, sqlite_db, table, dry_run, database_url)

    logger.info("Total rows migrated: %d", total_rows)
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
