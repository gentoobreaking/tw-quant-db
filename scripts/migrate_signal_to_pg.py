"""
T003: tw-quant-signal SQLite → PostgreSQL 移植腳本

將 signal 本地 SQLite 資料庫匯出到共享 PostgreSQL 的 signal schema。

使用方式:
  DATABASE_URL=postgresql://twquant:pwd@localhost:5432/twquant_shared \\
  SIGNAL_SQLITE_DB=/path/to/signal.db \\
  python scripts/migrate_signal_to_pg.py

原則:
- 逐表匯出: sqlite3 dump → pandas → PostgreSQL INSERT ON CONFLICT DO NOTHING
- stock_id 保持不變 (signal schema 使用 stock_id 而非 core 的 symbol)
- daily_prices/market_index/institutional_flows 可改為唯讀 core.* (後纫 Phase 3)
- 支援 --dry-run (僅顯示計畫不執行)
- 支援 --table TABLE (僅遷移指定表格)
"""

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

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


async def migrate_table(
    pg_conn, sqlite_db: str, table: str, dry_run: bool = False
) -> int:
    """將單一表從 SQLite 匯出到 PostgreSQL。

    使用 pandas + asyncpg 的 copy_records_to_table 進行高效批次插入。
    INSERT ON CONFLICT DO NOTHING — 支援中斷續跑。
    """
    # 讀取 SQLite 資料
    sqlite_conn = sqlite3.connect(sqlite_db)
    df = pd.read_sql_query(f"SELECT * FROM {table}", sqlite_conn)
    sqlite_conn.close()

    if len(df) == 0:
        logger.info("  %s: 0 rows, skipping", table)
        return 0

    # 取得 PostgreSQL 目標表的欄位 (驗證相容性)
    pg_cols = await pg_conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'signal' AND table_name = $1 "
        "ORDER BY ordinal_position",
        table,
    )
    pg_col_names = [row["column_name"] for row in pg_cols]

    # 確認 SQLite 欄位在 PG 表中存在
    sqlite_cols = list(df.columns)
    missing = set(sqlite_cols) - set(pg_col_names)
    if missing:
        logger.warning("  %s: 欄位缺失 — %s", table, missing)
        sqlite_cols = [c for c in sqlite_cols if c in pg_col_names]

    if not sqlite_cols:
        logger.warning("  %s: 無可對應欄位，跳過", table)
        return 0

    # 過濾 dataframe 欄位
    df = df[sqlite_cols]

    if dry_run:
        logger.info("  %s: DRY RUN — %d rows would be migrated", table, len(df))
        return len(df)

    # 批次插入臨時表 → 合併到正式表 (INSERT ON CONFLICT DO NOTHING)
    col_str = ", ".join(f'"{c}"' for c in sqlite_cols)
    count = 0
    for batch_start in range(0, len(df), 10000):
        batch = df.iloc[batch_start : batch_start + 10000]
        batch_records = batch.itertuples(index=False, name=None)

        await pg_conn.copy_records_to_table(
            f"_import_{table}",
            records=batch_records,
            columns=sqlite_cols,
            schema_name="signal",
        )
        count += len(batch)

    # 合併到正式表
    await pg_conn.execute(
        f'INSERT INTO signal."{table}" ({col_str}) '
        f'SELECT {col_str} FROM signal._import_{table} '
        f"ON CONFLICT DO NOTHING"
    )

    # 清空臨時表
    await pg_conn.execute(f"TRUNCATE TABLE signal._import_{table}")

    logger.info("  %s: migrated %d rows", table, count)
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
    from asyncpg import connect

    conn = await connect(database_url)
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

    # 建立 import 臨時表
    for table in tables_to_migrate:
        cols = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'signal' AND table_name = $1 "
            "ORDER BY ordinal_position",
            table,
        )
        if not cols:
            logger.warning("  %s: signal schema 中找不到，跳過", table)
            continue

        col_defs = ", ".join(
            f'"{row["column_name"]}" {row["data_type"]}' for row in cols
        )
        await conn.execute(
            f'CREATE TABLE IF NOT EXISTS signal._import_{table} ({col_defs})'
        )

    # 逐表遷移
    total_rows = 0
    for table in tables_to_migrate:
        total_rows += await migrate_table(conn, sqlite_db, table, dry_run)

    logger.info("Total rows migrated: %d", total_rows)
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
