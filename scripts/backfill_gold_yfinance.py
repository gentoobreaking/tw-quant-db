"""
T014: Backfill GOLD (comex gold futures, GC=F) historical prices from yfinance
into core.daily_prices (symbol='GOLD').

This data is needed for the gold-analysis frontend (T012/T014 verification).
All rows are marked source_role='FALLBACK' (not CANONICAL — tw-quant-mcp
is the canonical source but doesn't have GOLD data).
"""

import logging
import os
import sys
from datetime import datetime

import asyncpg
import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GOLD_SYMBOL = "GOLD"  # Symbol in core.daily_prices
YFINANCE_TICKER = "GC=F"  # COMEX Gold Futures
SOURCE = "yfinance_gc_futures"

async def main():
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://twquant:twquant-secret-password@localhost:5432/twquant_shared"
    )

    logger.info("Fetching GOLD futures data from yfinance: %s", YFINANCE_TICKER)
    gold = yf.Ticker(YFINANCE_TICKER)
    df = gold.history(period="max", interval="1d")
    logger.info("Fetched %d rows (date range: %s to %s)",
                len(df), df.index.min(), df.index.max())

    # Reset index to get 'Date' column
    df = df.reset_index()

    conn = await asyncpg.connect(database_url)
    logger.info("Connected to PostgreSQL")

    batch_size = 500
    inserted = 0
    for i in range(0, len(df), batch_size):
        batch = df.iloc[i:i+batch_size]
        rows = []
        for _, row in batch.iterrows():
            trade_date = row['Date'].date()
            open_price = float(row['Open'])
            high = float(row['High'])
            low = float(row['Low'])
            close = float(row['Close'])
            volume = int(row['Volume']) if not pd.isna(row['Volume']) else None
            # Calculate turnover (approximate: avg_price * volume)
            turnover = round(((open_price + close) / 2) * volume, 2) if volume else None

            rows.append((
                GOLD_SYMBOL,
                trade_date,
                round(open_price, 4),
                round(high, 4),
                round(low, 4),
                round(close, 4),
                round(close, 4),  # adjusted_close = close (no adjustments for futures)
                volume,
                turnover,
                SOURCE,
                trade_date,  # data_date
                "daily_close",  # freshness
                "FALLBACK",   # source_role
            ))

        await conn.executemany(
            """
            INSERT INTO core.daily_prices
                (symbol, trade_date, open, high, low, close, adjusted_close,
                 volume, turnover, source, data_date, freshness, source_role)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT DO NOTHING
            """,
            rows,
        )
        inserted += len(rows)
        logger.info("  %d / %d rows processed", inserted, len(df))

    # Verify
    count = await conn.fetchval(
        "SELECT count(*) FROM core.daily_prices WHERE symbol=$1", GOLD_SYMBOL
    )
    logger.info("Total GOLD rows in core.daily_prices: %d", count)
    logger.info("Backfill complete")
    await conn.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
