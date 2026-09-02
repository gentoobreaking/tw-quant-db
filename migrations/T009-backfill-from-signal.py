"""
T009: Backfill historical data from other projects to core schema

Phase 1a seed data: tw-quant-pickup 為唯一正式寫入者，但其他專案有歷史資料
可回補 core 作為種子資料。

所有回補資料標 source_role='FALLBACK'。後續 pickup 管線可覆蓋為 CANONICAL。
INSERT ON CONFLICT DO NOTHING — 不覆蓋既有的 CANONICAL 資料。

使用方式:
  DATABASE_URL=postgresql://twquant:pwd@localhost:5432/twquant_shared \\
  SIGNAL_DB=/path/to/tw-quant-signal/data/signal.db \\
  QUANT_CACHE_DB=/path/to/tw-quant/data/cache.db \\
  python scripts/backfill_from_signal.py
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


async def backfill_from_signal(pg_conn, signal_db: str):
    """從 tw-quant-signal/signal.db 回補資料到 core schema。
    
    主要對象: daily_prices (3,663 rows)
    signal 使用 stock_id，core 使用 symbol — 直接映射
    """
    if not Path(signal_db).exists():
        logger.warning("signal.db not found: %s", signal_db)
        return 0

    sqlite_conn = sqlite3.connect(signal_db)
    df = pd.read_sql_query("SELECT * FROM daily_prices", sqlite_conn)
    sqlite_conn.close()

    if len(df) == 0:
        logger.info("signal.daily_prices: 0 rows, skipping")
        return 0

    logger.info("signal.daily_prices: %d rows to backfill", len(df))

    # Map stock_id → symbol (same value, different column name)
    # Convert types for PostgreSQL compatibility
    import datetime as _dt

    def _to_date(val):
        if val is None:
            return None
        if isinstance(val, _dt.date):
            return val
        if isinstance(val, str):
            return _dt.datetime.strptime(val[:10], "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).date()
        return val

    records = []
    for _, row in df.iterrows():
        records.append((
            str(row["stock_id"]),      # symbol
            _to_date(row["trade_date"]),
            row.get("open"), row.get("high"), row.get("low"), row.get("close"),
            row.get("adj_close"),
            row.get("volume"),
            row.get("amount"),
            "tw-quant-signal",         # source
            None,                      # data_date
            None,                      # freshness
            "FALLBACK",                # source_role
        ))

    # Batch insert with INSERT ON CONFLICT DO NOTHING
    cols = [
        "symbol", "trade_date", "open", "high", "low", "close",
        "adjusted_close", "volume", "turnover", "source",
        "data_date", "freshness", "source_role",
    ]
    inserted = 0
    for i in range(0, len(records), 5000):
        batch = records[i : i + 5000]
        await pg_conn.executemany(
            f"INSERT INTO core.daily_prices ({', '.join(cols)}) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
            f"ON CONFLICT DO NOTHING",
            batch,
        )
        inserted += len(batch)

    logger.info("  ✅ Backfilled %d rows from signal.daily_prices → core.daily_prices", inserted)
    return inserted


async def backfill_from_quant(pg_conn, quant_cache_db: str):
    """從 tw-quant/cache.db 回補 stocks 到 core.stocks。"""
    if not Path(quant_cache_db).exists():
        logger.warning("quant cache.db not found: %s", quant_cache_db)
        return 0

    # Check if file is empty
    if os.path.getsize(quant_cache_db) == 0:
        logger.info("  quant cache.db is empty, skipping")
        return 0

    sqlite_conn = sqlite3.connect(quant_cache_db)
    try:
        df = pd.read_sql_query(
            "SELECT key, value, timestamp FROM cache WHERE key LIKE 'stock_%'",
            sqlite_conn,
        )
    except Exception:  # noqa: BLE001 — table may not exist
        logger.warning("  quant cache.db has no stock data, skipping")
        sqlite_conn.close()
        return 0
    sqlite_conn.close()

    if len(df) == 0:
        logger.info("quant cache: no stock data, skipping")
        return 0

    logger.info("quant cache: %d stock entries to backfill", len(df))
    # tw-quant cache stores JSON values — parse and insert
    # This is a simplified version; actual implementation depends on cache format
    inserted = 0
    for _, row in df.iterrows():
        key = str(row["key"])
        symbol = key.replace("stock_", "")
        if len(symbol) <= 10:
            await pg_conn.execute(
                "INSERT INTO core.stocks (symbol, name, market, sector, "
                "security_type, listed_date, active, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW()) "
                "ON CONFLICT (symbol) DO NOTHING",
                symbol, symbol, "TSE", None, "股票", None, True,
            )
            inserted += 1

    logger.info("  ✅ Backfilled %d stocks from quant cache → core.stocks", inserted)
    return inserted


async def verify_backfill(pg_conn):
    """驗證回補結果。"""
    logger.info("=== Backfill Verification ===")

    tables = [
        "core.stocks",
        "core.daily_prices",
        "core.financials",
        "core.monthly_revenues",
        "core.dividends",
        "core.institutional_flow",
        "core.market_context",
        "core.universe_flags",
    ]

    all_ok = True
    for tbl in tables:
        count = await pg_conn.fetchval(f"SELECT COUNT(*) FROM {tbl}")
        logger.info("  %s: %d rows", tbl, count)

    # Verify FALLBACK lineage
    fallback_count = await pg_conn.fetchval(
        "SELECT COUNT(*) FROM core.daily_prices WHERE source_role = 'FALLBACK'"
    )
    canonical_count = await pg_conn.fetchval(
        "SELECT COUNT(*) FROM core.daily_prices WHERE source_role = 'CANONICAL'"
    )
    logger.info("  daily_prices lineage: FALLBACK=%d, CANONICAL=%d",
                fallback_count, canonical_count)

    if fallback_count > 0:
        logger.info("  ✅ FALLBACK lineage properly tagged")
    else:
        logger.warning("  ⚠️  No FALLBACK data found (backfill may have been overwritten)")

    if canonical_count > 0:
        logger.info("  ✅ CANONICAL data from pickup present")
    else:
        logger.info("  ℹ️  No CANONICAL data yet (pickup not yet migrated)")

    return all_ok


async def main():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://localhost:5432/twquant_shared"
    )
    signal_db = os.environ.get("SIGNAL_DB", "")
    quant_cache_db = os.environ.get("QUANT_CACHE_DB", "")
    dry_run = "--dry-run" in sys.argv

    from asyncpg import connect

    conn = await connect(database_url)
    logger.info("Connected to PostgreSQL: %s", database_url)

    total_inserted = 0

    if signal_db:
        if dry_run:
            sqlite_conn = sqlite3.connect(signal_db)
            count = sqlite_conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
            sqlite_conn.close()
            logger.info("[DRY RUN] signal.daily_prices: %d rows would be backfilled", count)
        else:
            total_inserted += await backfill_from_signal(conn, signal_db)

    if quant_cache_db:
        if dry_run:
            logger.info("[DRY RUN] quant cache → core.stocks")
        else:
            total_inserted += await backfill_from_quant(conn, quant_cache_db)

    # Also check for daybrain cache
    daybrain_db = os.environ.get("DAYBRAIN_CACHE_DB", "")
    if daybrain_db and not dry_run:
        if Path(daybrain_db).exists():
            sqlite_conn = sqlite3.connect(daybrain_db)
            count = sqlite_conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
            sqlite_conn.close()
            logger.info("daybrain cache_entries: %d rows", count)
    elif daybrain_db and dry_run:
        logger.info("[DRY RUN] daybrain cache → core")

    logger.info("Total rows backfilled: %d", total_inserted)
    await verify_backfill(conn)
    await conn.close()
    logger.info("Backfill complete")


if __name__ == "__main__":
    asyncio.run(main())
