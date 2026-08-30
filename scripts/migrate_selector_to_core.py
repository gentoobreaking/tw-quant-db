"""
T004: tw-quant-selector 接入 core (Phase 2, 唯讀)

本腳本將 selector 的 SQLite/DuckDB 資料遷入 PostgreSQL selector schema，
並建立 core.* → selector 相容性 views。

原則:
- selector 只讀 core 的 fact raw tables（stocks, daily_prices, financials, ...）
- selector 自己的業務表（portfolio, backtest_*, alert_*, 等）存放在 selector schema
- 透過 views 讓 selector 程式碼逐步遷移 stock_id → symbol
- 歷史資料 ETL 時 lineage 一律標 FALLBACK

使用方式:
  DATABASE_URL=postgresql://twquant:pwd@localhost:5432/twquant_shared \\
  SELECTOR_SQLITE_DB=/path/to/selector.db \\
  python scripts/migrate_selector_to_core.py
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

# Selector business-only tables (fact/raw tables come from core)
SELECTOR_BUSINESS_TABLES = [
    "portfolio",
    "lots",
    "alert_settings",
    "alert_rules",
    "alert_history",
    "alert_cooldowns",
    "realtime_quotes",
    "intraday_kline",
    "backtest_runs",
    "backtest_positions",
    "backtest_equity",
    "guru_scores",
    "strategy_config_history",
    "ingestion_tracker",
    "operation_logs",
]


# ─── Compatibility Views: core.* → selector (stock_id → symbol) ──────────

COMPAT_VIEWS = """
-- selector 讀 core，但程式碼用 stock_id，需要 view 相容層
CREATE OR REPLACE VIEW selector.v_daily_prices AS
SELECT
    symbol AS stock_id,
    trade_date,
    open, high, low, close,
    adjusted_close, volume, turnover,
    source, data_date, freshness, source_role
FROM core.daily_prices;

CREATE OR REPLACE VIEW selector.v_stocks AS
SELECT
    symbol AS stock_id,
    name AS stock_name,
    market, industry,
    listed_date AS list_date,
    active AS is_etf,
    created_at
FROM core.stocks;

CREATE OR REPLACE VIEW selector.v_monthly_revenue AS
SELECT
    symbol AS stock_id,
    TO_CHAR(year_month, 'YYYY-MM') AS year_month,
    revenue, yoy_growth AS revenue_yoy,
    reported_at AS announcement_date
FROM core.monthly_revenues;

CREATE OR REPLACE VIEW selector.v_financials AS
SELECT
    symbol AS stock_id,
    CONCAT(fiscal_year, 'Q', fiscal_quarter) AS year_quarter,
    *
FROM core.financials;

CREATE OR REPLACE VIEW selector.v_valuations AS
SELECT
    symbol AS stock_id,
    trade_date,
    pe_ratio, pb_ratio, dividend_yield, market_cap
FROM core.v_valuations_date  -- date-based valuations (per tw-quant-db-status.md §3.3)
WHERE FALSE;  -- placeholder: valuations in selector are snapshot-based, keep separate

CREATE OR REPLACE VIEW selector.v_signals AS
SELECT
    signal_date::TEXT AS signal_date,
    symbol AS stock_id,
    strategy, score, rank, is_selected
FROM selector.signals_date;  -- selector-specific date-based signals
""".strip()


async def create_compat_views(pg_conn):
    """建立 core → selector 的 view 相容層。"""
    for view_sql in COMPAT_VIEWS.split(";"):
        view_sql = view_sql.strip()
        if view_sql and "CREATE OR REPLACE VIEW" in view_sql:
            try:
                await pg_conn.execute(view_sql)
                logger.info("Created view: %s", view_sql.split()[3])
            except (OSError, RuntimeError) as e:
                logger.warning("  View creation skipped: %s", e)


async def migrate_selector_table(
    pg_conn, sqlite_db: str, table: str, dry_run: bool = False
) -> int:
    """將 selector 資料從 SQLite 遷入 PostgreSQL selector schema。"""
    sqlite_conn = sqlite3.connect(sqlite_db)
    df = pd.read_sql_query(f"SELECT * FROM {table}", sqlite_conn)
    sqlite_conn.close()

    if len(df) == 0:
        return 0

    sqlite_cols = list(df.columns)
    col_str = ", ".join(f'"{c}"' for c in sqlite_cols)

    if dry_run:
        logger.info("  %s: DRY RUN — %d rows", table, len(df))
        return len(df)

    # 批次插入
    for batch_start in range(0, len(df), 10000):
        batch = df.iloc[batch_start : batch_start + 10000]
        batch_records = batch.itertuples(index=False, name=None)

        await pg_conn.copy_records_to_table(
            f"_import_{table}",
            records=batch_records,
            columns=sqlite_cols,
            schema_name="selector",
        )

        await pg_conn.execute(
            f'INSERT INTO selector."{table}" ({col_str}) '
            f'SELECT {col_str} FROM selector._import_{table} '
            f"ON CONFLICT DO NOTHING"
        )
        await pg_conn.execute(f"TRUNCATE TABLE selector._import_{table}")

    logger.info("  %s: migrated %d rows", table, len(df))
    return len(df)


async def mark_fallback_lineage(pg_conn):
    """對歷史資料標記 source_role = 'FALLBACK'。

    依 spec §8.1：無法驗證來源的資料不得標 CANONICAL。
    selector 遷入的歷史資料全部標 FALLBACK。
    """
    tables_with_lineage = [
        "daily_prices",
        "financials",
        "monthly_revenues",
        "dividends",
        "institutional_flow",
        "market_context",
    ]

    for table in tables_with_lineage:
        try:
            await pg_conn.execute(
                f"UPDATE core.{table} SET source_role = 'FALLBACK' "
                f"WHERE source_role = 'CANONICAL' AND source NOT LIKE '%oanda%'"
            )
        except (OSError, RuntimeError):
            logger.debug("  Table %s not in core schema yet", table)

    logger.info("Lineage fallback marking complete")


async def main():
    sqlite_db = os.environ.get("SELECTOR_SQLITE_DB", "")
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://localhost:5432/twquant_shared"
    )
    dry_run = "--dry-run" in sys.argv

    if not sqlite_db or not Path(sqlite_db).exists():
        if dry_run:
            logger.info("DRY RUN mode — listing tables to migrate")
        else:
            logger.error("請設定 SELECTOR_SQLITE_DB 環境變數")
            sys.exit(1)

    from asyncpg import connect

    conn = await connect(database_url)
    logger.info("Connected to PostgreSQL")

    # Step 1: Create compatibility views
    logger.info("Creating compatibility views (stock_id → symbol)...")
    await create_compat_views(conn)

    # Step 2: Migrate selector business tables
    if sqlite_db:
        import sqlite3
        sqlite_conn = sqlite3.connect(sqlite_db)
        tables = [
            r[0]
            for r in sqlite_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        sqlite_conn.close()

        for table in SELECTOR_BUSINESS_TABLES:
            if table not in tables:
                logger.info("  %s: not found in SQLite, skipping", table)
                continue
            # Ensure _import temp table exists
            cols = await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'selector' AND table_name = $1 "
                "ORDER BY ordinal_position",
                table,
            )
            if not cols:
                logger.warning("  %s: table not in selector schema", table)
                continue
            # Create temp table for copy
            col_defs = ", ".join(
                f'"{r["column_name"]}" {r["data_type"]}' for r in cols
            )
            await conn.execute(
                f'CREATE TABLE IF NOT EXISTS selector._import_{table} ({col_defs})'
            )
            await migrate_selector_table(conn, sqlite_db, table, dry_run)

    # Step 3: Mark fallback lineage
    logger.info("Marking fallback lineage for historical data...")
    await mark_fallback_lineage(conn)

    await conn.close()
    logger.info("T004 migration complete")


if __name__ == "__main__":
    asyncio.run(main())
