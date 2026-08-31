#!/usr/bin/env python3
"""tw-quant-db core daily_prices backfill engine.

Fallback chain: local-mcp → twse-online → finmind-mcp → yfinance-mcp
Idempotent: ON CONFLICT DO UPDATE ensures safe re-runs.
"""
import os, sys, asyncio, logging
from datetime import date, timedelta, datetime

import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def load_stock_list() -> list[str]:
    """Load watchlist from env: STOCK_IDS, STOCKS_FILE, or BACKFILL_ALL_LISTED."""
    if ids := os.environ.get("STOCK_IDS"):
        return [s.strip() for s in ids.split(",") if s.strip()]
    if fpath := os.environ.get("STOCKS_FILE") and os.path.exists(fpath):
        with open(fpath) as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    if os.environ.get("BACKFILL_ALL_LISTED", "").lower() == "true":
        return fetch_all_listed()
    return ["2330", "0050", "2317"]  # Default test set


def fetch_all_listed() -> list[str]:
    """Fetch all TWSE/OTC stocks — stub for now."""
    logger.info("Fetching full stock list... (not implemented)")
    return ["2330", "0050", "2317", "2308", "2303"]


def get_trading_days(start: date, end: date) -> list[date]:
    """Generate trading days excluding weekends."""
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def get_missing_dates(conn, stock_id: str, start: date, end: date) -> list[date]:
    """Query DB for missing trade_dates in range."""
    existing = set()
    cur = conn.cursor()
    cur.execute(
        "SELECT trade_date FROM core.daily_prices WHERE symbol = %s AND trade_date BETWEEN %s AND %s",
        (stock_id, start, end)
    )
    for row in cur:
        existing.add(row[0])

    missing = []
    for d in get_trading_days(start, end):
        if d not in existing:
            missing.append(d)
    return missing


async def backfill_stock(conn, stock_id: str, dates: list[date], dry_run: bool = False) -> int:
    """Backfill missing dates for one stock using fallback sources."""
    if not dates:
        logger.info(f"{stock_id}: No missing dates.")
        return 0

    total_inserted = 0
    # Batch by 5 trading days to respect upstream limits
    for i in range(0, len(dates), 5):
        batch = dates[i:i+5]
        start, end = batch[0], batch[-1]

        data = None
        used_source = None
        # Try each source in order
        for src in SOURCES:
            try:
                res = await asyncio.wait_for(src.fetch(stock_id, start, end), timeout=30)
                if res.data and len(res.data) > 0:
                    returned_dates = set(r['trade_date'] for r in res.data)
                    coverage = len(returned_dates) / len(batch)
                    if coverage >= 0.7:
                        data = res.data
                        used_source = src.name
                        logger.info(f"{stock_id}: Got {len(data)} rows from {src.name} ({coverage:.0%} coverage)")
                        break
                    else:
                        logger.warning(f"{stock_id}: {src.name} incomplete ({coverage:.0%}), trying next")
                else:
                    logger.warning(f"{stock_id}: {src.name} returned no data")
            except asyncio.TimeoutError:
                logger.warning(f"{stock_id}: {src.name} timed out (>30s)")
            except Exception as e:
                logger.warning(f"{stock_id}: {src.name} failed: {e}")

        if data:
            if not dry_run:
                inserted = upsert_prices(conn, stock_id, data)
                total_inserted += inserted
            else:
                logger.info(f"[DRY-RUN] Would insert {len(data)} rows for {stock_id}")
        else:
            logger.error(f"{stock_id}: All sources exhausted for batch {start} to {end}")

    return total_inserted


def upsert_prices(conn, stock_id: str, rows: list[dict]) -> int:
    """Insert or update core.daily_prices."""
    cur = conn.cursor()
    count = 0
    for r in rows:
        cur.execute("""
            INSERT INTO core.daily_prices 
              (symbol, trade_date, open, high, low, close, volume, adjusted_close)
            VALUES (%(symbol)s, %(trade_date)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s, %(adjusted_close)s)
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                adjusted_close = EXCLUDED.adjusted_close
        """, {**r, "symbol": stock_id})
        count += cur.rowcount or 0
    conn.commit()
    return count


# ---- Source Definitions ----
from dataclasses import dataclass

@dataclass
class SourceResult:
    name: str
    data: list[dict]
    score: float

class BaseSource:
    name: str
    weight: float
    async def fetch(self, stock_id: str, start: date, end: date) -> SourceResult:
        raise NotImplementedError

class LocalMCPSource(BaseSource):
    name = "local-mcp"
    weight = 1.0
    async def fetch(self, stock_id: str, start: date, end: date) -> SourceResult:
        raise NotImplementedError  # TODO: integrate MCP client

SOURCES: list[BaseSource] = [
    LocalMCPSource(),
    # TWSEOnlineSource(),
    # FinMindSource(),
    # YFinanceSource(),
]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backfill core.daily_prices")
    parser.add_argument("--start", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    parser.add_argument("--end", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    parser.add_argument("--stock-ids", type=str)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stocks = load_stock_list()
    if args.stock_ids:
        stocks = [s.strip() for s in args.stock_ids.split(",")]

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    with psycopg.connect(dsn) as conn:
        total = 0
        for sid in stocks:
            if args.auto or (not args.start or not args.end):
                last = conn.execute(
                    "SELECT MAX(trade_date) FROM core.daily_prices WHERE symbol = %s", (sid,)
                ).fetchone()
                start = last[0] + timedelta(days=1) if last and last[0] else date(2024, 1, 1)
                end = date.today()
            else:
                start, end = args.start, args.end
            missing = get_missing_dates(conn, sid, start, end)
            inserted = asyncio.run(backfill_stock(conn, sid, missing, dry_run=args.dry_run))
            total += inserted
            logger.info(f"{sid}: Backfilled {len(missing)} dates, inserted {inserted} rows")
        logger.info(f"Total rows processed: {total}")
