"""
T017: Backfill historical data from tw-quant-mcp cache.db → core schema

從 tw-quant-mcp 的 cache.db 回補 4,818 筆資料到 core schema。
所有資料標 source_role='FALLBACK'。

Data formats in cache_entries.value:
- Raw bytes (JSON string as bytes) — e.g. daily_kline, dividend, calendar
- Double-encoded: bytes containing a JSON string literal with base64-encoded JSON
- Plain string (JSON)

Usage:
  DATABASE_URL=postgresql://twquant:pwd@localhost:5432/twquant_shared \\
  MCP_CACHE_DB=/path/to/tw-quant-mcp/data/cache.db \\
  python3 scripts/backfill_from_mcp.py
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _to_date(val) -> date | None:
    """Convert string/date to date object.

    Handles:
    - datetime/date objects
    - ISO format: "2026-08-18"
    - TW date format: "1150825" (民國 year + MMDD) → 2026-08-25
    - YYYYMMDD format: "20260825"
    """
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        val = val.strip()
        # Handle TW date format: 1150825 → year=115+1911=2026, month=08, day=25
        if len(val) == 7 and val.isdigit():
            try:
                tw_year = int(val[:3])
                month = int(val[3:5])
                day = int(val[5:7])
                return date(tw_year + 1911, month, day)
            except (ValueError, TypeError):
                pass
        # Handle YYYYMMDD format
        if len(val) == 8 and val.isdigit():
            try:
                return date(int(val[:4]), int(val[4:6]), int(val[6:8]))
            except (ValueError, TypeError):
                pass
        # Standard ISO format
        try:
            return datetime.strptime(val[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).date()
        except ValueError:
            return None
    return None


def _to_numeric(val):
    """Convert value to numeric, handling string '--' and whitespace."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        val = val.strip().replace(",", "")
        if val in ("", "--", "N/A", "null", "None"):
            return None
        try:
            return float(val)
        except ValueError:
            return None
    return None


def _to_numeric_bounded(val, max_digits=999999.9999):
    """Convert value to numeric, clamping to fit NUMERIC(10,4) column.

    Values exceeding the column's range are returned as None (likely data outliers).
    """
    num = _to_numeric(val)
    if num is None:
        return None
    if abs(num) > max_digits:
        return None
    return num


def _parse_json_list(data: str) -> list | None:
    """Parse JSON string, handle double-encoded values, wrap non-list to list."""
    if not data:
        return None
    records = json.loads(data)
    # If decoded result is a string (JSON string literal), try base64 decode
    if isinstance(records, str):
        try:
            decoded = base64.b64decode(records).decode("utf-8")
            records = json.loads(decoded)
        except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise json.JSONDecodeError("Not valid JSON or base64", data, 0)
    if not isinstance(records, list):
        records = [records]
    return records
def _decode_cache_value(value_blob) -> list | None:
    """Decode a cache_entries.value to list of dicts.

    Value can be stored as:
    1. Raw bytes (JSON string as bytes) — e.g. daily_kline, dividend
    2. Bytes containing JSON string literal with base64-encoded JSON — e.g. financials
    3. Plain string (JSON)
    """
    try:
        if isinstance(value_blob, str):
            return _parse_json_list(value_blob)
        elif isinstance(value_blob, bytes):
            # Try raw JSON decode first
            try:
                data = value_blob.decode("utf-8")
                return _parse_json_list(data)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                pass
            # Fall back to base64 decode
            decoded = base64.b64decode(value_blob).decode("utf-8")
            return _parse_json_list(decoded)
        return None
    except (ValueError, json.JSONDecodeError, TypeError, binascii.Error):
        return None


def _tw_year_month_to_date(tw_year_month: str) -> date | None:
    """Convert TW year-month (e.g. "11507") → date(2026, 7, 1)."""
    if not isinstance(tw_year_month, str) or len(tw_year_month) < 5 or not tw_year_month.isdigit():
        return None
    try:
        tw_year = int(tw_year_month[:3])
        month = int(tw_year_month[3:5])
        return date(tw_year + 1911, month, 1)
    except (ValueError, TypeError):
        return None


def _reverse_daily_kline_key(
    cache_key: str, data_date: str, stock_codes: list[str],
    month_starts: list[str] | None = None,
) -> str | None:
    """Reverse a daily_kline cache key to find the stock code.

    Key format (§4.3): sha256("TWSE_WEB|daily_k|data_date|symbol|params_hash")[:16]
    params_hash = sha256("date=YYYYMMDD&stockNo=code")[:16]
    where date=YYYYMMDD is the month-start (first day of the month).

    If month_starts is provided (from the data's timestamps), only those
    month-start dates are tried — much faster than brute-forcing all months.
    Also tries the data_date itself as a params date (some entries use the
    request date, not the month start, as the date parameter).
    """
    source = "TWSE_WEB"
    ds = "daily_k"
    if month_starts is None:
        # Default: try all month-starts from Jan 2025 to Dec 2026
        month_starts = [f"{ym:06d}01" for ym in range(202501, 202701)]
    # Also try the data_date itself as params date (YYYYMMDD format)
    if data_date:
        month_starts = list(month_starts) + [data_date.replace("-", "")]
    for code in stock_codes:
        for ymd in month_starts:
            params = {"date": ymd, "stockNo": code}
            sorted_keys = sorted(params.keys())
            params_str = "&".join(f"{k}={params[k]}" for k in sorted_keys)
            ph = hashlib.sha256(params_str.encode()).hexdigest()[:16]
            payload = f"{source}|{ds}|{data_date}|{code}|{ph}"
            computed = hashlib.sha256(payload.encode()).hexdigest()[:16]
            if computed == cache_key:
                return code
    return None

async def backfill_financials(pg_conn, sqlite_conn) -> int:
    """回補 tw-quant-mcp financials → core.financials."""
    rows = sqlite_conn.execute(
        "SELECT data_date, value FROM cache_entries WHERE dataset='financials'"
    ).fetchall()
    logger.info("financials: %d entries to process", len(rows))

    inserted = 0
    skipped = 0
    for data_date, value_blob in rows:
        records = _decode_cache_value(value_blob)
        if records is None:
            logger.warning("  skip invalid financials entry for date %s", data_date)
            continue

        batch = []
        for r in records:
            if not isinstance(r, dict):
                continue
            symbol = str(r.get("code", r.get("公司代號", "")))
            if not symbol:
                skipped += 1
                continue
            # Handle both English and Chinese field names
            fiscal_year = r.get("year", r.get("年度"))
            fiscal_quarter = r.get("quarter", r.get("季別"))
            # Infer from table_date if missing
            table_date_str = r.get(
                "table_date", r.get("出表日期", r.get("data_date", data_date))
            )
            # Handle TW year (民國年) - "115" → 2026
            if isinstance(fiscal_year, str) and fiscal_year.isdigit():
                fiscal_year = int(fiscal_year)
                if fiscal_year < 1900:
                    fiscal_year += 1911
            if isinstance(fiscal_quarter, str):
                try:
                    fiscal_quarter = int(fiscal_quarter)
                except ValueError:
                    fiscal_quarter = None
            if fiscal_year is None and table_date_str:
                try:
                    fiscal_year = int(str(table_date_str)[:4])
                except (ValueError, TypeError):
                    pass
            if fiscal_quarter is None and table_date_str:
                try:
                    month = int(str(table_date_str)[5:7])
                    fiscal_quarter = (month - 1) // 3 + 1
                except (ValueError, TypeError):
                    pass
            # Skip if still missing required fields
            if fiscal_year is None or fiscal_quarter is None:
                skipped += 1
                continue
            batch.append((
                symbol,
                int(fiscal_year),
                int(fiscal_quarter),
                0,  # revision
                _to_numeric(r.get("revenue", r.get("營收"))),
                _to_numeric(r.get("gross_profit", r.get("毛利"))),
                _to_numeric(r.get("operating_profit", r.get("營業利益"))),
                _to_numeric(r.get("net_income", r.get("淨利"))),
                _to_numeric(r.get("eps", r.get("EPS"))),
                _to_numeric(r.get("book_value_per_share")),
                _to_numeric(r.get("total_assets", r.get("總資產"))),
                _to_numeric(r.get("total_liabilities", r.get("總負債"))),
                _to_numeric(r.get("equity", r.get("權益"))),
                _to_numeric(r.get("roe", r.get("ROE"))),
                _to_numeric(r.get("roa", r.get("ROA"))),
                _to_numeric(r.get("operating_cash_flow")),
                _to_numeric(r.get("investing_cash_flow")),
                _to_numeric(r.get("capex")),
                _to_numeric(r.get("free_cash_flow")),
                _to_date(table_date_str),
                _to_date(data_date),
                "tw-quant-mcp",
                "FALLBACK",
            ))

        if batch:
            await pg_conn.executemany(
                "INSERT INTO core.financials "
                "(symbol, fiscal_year, fiscal_quarter, revision, revenue, "
                "gross_profit, operating_income, net_income, eps, book_value_per_share, "
                "total_assets, total_liabilities, equity, roe, roa, "
                "operating_cash_flow, investing_cash_flow, capex, free_cash_flow, "
                "reported_at, observed_at, source, source_role) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, "
                "$11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23) "
                "ON CONFLICT DO NOTHING",
                batch,
            )
            inserted += len(batch)

    logger.info("  ✅ Backfilled %d financials → core.financials (skipped: %d)", inserted, skipped)
    return inserted


async def backfill_daily_kline(pg_conn, sqlite_conn) -> int:
    """回補 tw-quant-mcp daily_kline → core.daily_prices.

    daily_kline entries have two types:
    - Candle data (timestamp/open/high/low/close/volume/amount) — no stock code in value
      → reverse cache key using TWSE_WEB|daily_k|data_date|symbol|params_hash format
    - Market snapshot (code/name/volume/open/high/low/close) — stock code in value
      → use directly (these are the daily market close snapshots)
    - Other types (index/maint/stats) — skip, not stock-level data
    """
    # Get stock codes from calendar for key reversal
    cal_rows = sqlite_conn.execute(
        "SELECT value FROM cache_entries WHERE dataset='calendar'"
    ).fetchall()
    stock_codes = set()
    for (value_blob,) in cal_rows:
        records = _decode_cache_value(value_blob)
        if records is None:
            continue
        for r in records:
            if not isinstance(r, dict):
                continue
            code = str(r.get("code", ""))
            if code and code.isdigit():
                stock_codes.add(code)
    stock_codes = sorted(stock_codes)
    logger.info("  Loaded %d stock codes from calendar for key reversal", len(stock_codes))

    rows = sqlite_conn.execute(
        "SELECT key, data_date, value FROM cache_entries WHERE dataset='daily_kline'"
    ).fetchall()
    logger.info("daily_kline: %d entries to process", len(rows))

    inserted = 0
    skipped = 0
    for cache_key, data_date, value_blob in rows:
        records = _decode_cache_value(value_blob)
        if records is None:
            logger.warning("  skip invalid daily_kline entry: key=%s", cache_key)
            skipped += 1
            continue

        if not records or not isinstance(records[0], dict):
            skipped += 1
            continue

        first = records[0]
        keys_set = set(first.keys())

        # Determine symbol: either from value or key reversal
        symbol = str(first.get("code", first.get("symbol", "")))

        if not symbol:
            # Try key reversal for candle data (timestamp/open/high/low/close/volume/amount)
            if "timestamp" in keys_set and "open" in keys_set:
                # Extract month-start dates from timestamps for targeted search
                timestamps = [r.get("timestamp", "") for r in records if isinstance(r, dict)]
                month_starts = sorted(set(
                    ts.replace("-", "")[:6] + "01"
                    for ts in timestamps if isinstance(ts, str) and len(ts) >= 10
                ))
                if not month_starts:
                    month_starts = [f"{ym:06d}01" for ym in range(202501, 202701)]
                symbol = _reverse_daily_kline_key(cache_key, data_date, stock_codes, month_starts)
                if symbol:
                    logger.info("  reversed key %s → symbol=%s", cache_key, symbol)
                else:
                    logger.warning("  could not reverse key %s (candle data, no stock code)", cache_key)
                    skipped += 1
                    continue
            else:
                # Other data types (index/maint/stats) — skip
                skipped += 1
                continue

        batch = []
        for r in records:
            if not isinstance(r, dict):
                continue
            ts = _to_date(r.get("timestamp", r.get("date", "")))
            if ts is None:
                continue
            batch.append((
                symbol,
                ts,
                _to_numeric(r.get("open")),
                _to_numeric(r.get("high")),
                _to_numeric(r.get("low")),
                _to_numeric(r.get("close")),
                _to_numeric(r.get("adj_close", r.get("close"))),
                _to_numeric(r.get("volume")),
                _to_numeric(r.get("amount")),
                "tw-quant-mcp",
                ts,
                "raw",
                "FALLBACK",
            ))

        if batch:
            await pg_conn.executemany(
                "INSERT INTO core.daily_prices "
                "(symbol, trade_date, open, high, low, close, adjusted_close, "
                "volume, turnover, source, data_date, freshness, source_role) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) "
                "ON CONFLICT DO NOTHING",
                batch,
            )
            inserted += len(batch)

    logger.info("  ✅ Backfilled %d daily_kline → core.daily_prices (skipped: %d)", inserted, skipped)
    return inserted

async def backfill_dividends(pg_conn, sqlite_conn) -> int:
    """回補 tw-quant-mcp dividend → core.dividends."""
    rows = sqlite_conn.execute(
        "SELECT value FROM cache_entries WHERE dataset='dividend'"
    ).fetchall()
    logger.info("dividend: %d entries to process", len(rows))

    inserted = 0
    for (value_blob,) in rows:
        records = _decode_cache_value(value_blob)
        if records is None:
            continue

        batch = []
        for r in records:
            if not isinstance(r, dict):
                continue
            symbol = str(r.get("code", ""))
            if not symbol:
                continue
            dy = r.get("dividend_year", r.get("year", r.get("年度")))
            if isinstance(dy, str) and dy.isdigit():
                dy = int(dy)
                if dy < 1900:
                    dy += 1911
            batch.append((
                symbol,
                dy,
                _to_numeric(r.get("cash_dividend")),
                _to_numeric(r.get("stock_dividend")),
                _to_numeric(r.get("cash_yield", r.get("payout_ratio"))),
                _to_date(r.get("ex_date")),
                _to_date(r.get("payment_date")),
                "tw-quant-mcp",
                _to_date(r.get("table_date")),
                "raw",
                "FALLBACK",
            ))

        if batch:
            await pg_conn.executemany(
                "INSERT INTO core.dividends "
                "(symbol, fiscal_year, cash_dividend, stock_dividend, payout_ratio, "
                "ex_date, payment_date, source, data_date, freshness, source_role) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
                "ON CONFLICT DO NOTHING",
                batch,
            )
            inserted += len(batch)

    logger.info("  ✅ Backfilled %d dividends → core.dividends", inserted)
    return inserted


async def backfill_monthly_revenue(pg_conn, sqlite_conn) -> int:
    """回補 tw-quant-mcp monthly_revenue → core.monthly_revenues."""
    rows = sqlite_conn.execute(
        "SELECT value FROM cache_entries WHERE dataset='monthly_revenue'"
    ).fetchall()
    logger.info("monthly_revenue: %d entries to process", len(rows))

    inserted = 0
    for (value_blob,) in rows:
        records = _decode_cache_value(value_blob)
        if records is None:
            continue

        batch = []
        for r in records:
            if not isinstance(r, dict):
                continue
            # Handle both English and Chinese field names
            symbol = str(r.get("code", r.get("公司代號", "")))
            if not symbol:
                continue
            # 資料年月 is TW year-month (e.g. "11507" = 2026-07); also try English
            ym = r.get("data_year_month", r.get("資料年月", ""))
            year_month_date = _tw_year_month_to_date(ym)
            if year_month_date is None:
                year_month_date = _to_date(ym)
            if year_month_date is None:
                continue
            # reported_at (出表日期) is TW date format (e.g. "1150817" = 2026-08-17)
            reported_at = _to_date(r.get("report_date", r.get("table_date", r.get("出表日期", ""))))
            batch.append((
                symbol,
                year_month_date,
                _to_numeric(r.get("revenue", r.get("營業收入-當月營收"))),
                _to_numeric_bounded(r.get("yoy_change_pct", r.get("營業收入-去年同月增減(%)"))),
                _to_numeric_bounded(r.get("mom_change_pct", r.get("營業收入-上月比較增減(%)"))),
                _to_numeric(r.get("cum_revenue", r.get("cumulative_revenue", r.get("累計營業收入-當月累計營收")))),
                reported_at,
                _NOW,
                "tw-quant-mcp",
                _to_date(r.get("data_date")),
                "raw",
                "FALLBACK",
            ))

        if batch:
            await pg_conn.executemany(
                "INSERT INTO core.monthly_revenues "
                "(symbol, year_month, revenue, yoy_growth, mom_growth, "
                "cumulative_revenue, reported_at, observed_at, source, "
                "data_date, freshness, source_role) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) "
                "ON CONFLICT DO NOTHING",
                batch,
            )
            inserted += len(batch)

    logger.info("  ✅ Backfilled %d monthly_revenue → core.monthly_revenues", inserted)
    return inserted


async def backfill_institutional(pg_conn, sqlite_conn) -> int:
    """回補 tw-quant-mcp institutional + foreign_holding → core.institutional_flow."""
    total_inserted = 0
    for dataset_name in ["institutional", "foreign_holding"]:
        rows = sqlite_conn.execute(
            f"SELECT data_date, value FROM cache_entries WHERE dataset='{dataset_name}'"
        ).fetchall()
        logger.info("%s: %d entries to process", dataset_name, len(rows))

        for data_date, value_blob in rows:
            records = _decode_cache_value(value_blob)
            if records is None:
                continue

            batch = []
            for r in records:
                if not isinstance(r, dict):
                    continue
                # Handle both English and Chinese field names
                symbol = str(r.get("code", r.get("symbol", r.get("股票代號", ""))))
                if not symbol:
                    continue
                # Handle date field names: date, _date, trade_date
                trade_date = _to_date(r.get("date", r.get("_date", r.get("trade_date", ""))))
                if trade_date is None:
                    trade_date = _to_date(data_date)
                # availability_date is NOT NULL — use data_date as fallback
                availability_date = _to_date(r.get("data_date", r.get("date", data_date)))
                if availability_date is None:
                    availability_date = _to_date(data_date)
                if availability_date is None:
                    availability_date = _NOW.date()

                # Map fields for both datasets
                # institutional: foreign_net, investment_trust_net, dealer_net (already in value)
                # foreign_holding: has foreign_shares, foreign_percent, etc. — not flow data
                # but also has foreign_net in some entries
                f_net = _to_numeric(
                    r.get("foreign_net", r.get("foreign_buy", None))
                )
                it_net = _to_numeric(
                    r.get("investment_trust_net", r.get("investment_net", None))
                )
                dealer_net = _to_numeric(
                    r.get("dealer_net", None)
                )

                # For foreign_holding entries that only have foreign_shares (not net flow),
                # skip — they're holding data not flow data
                if dataset_name == "foreign_holding" and f_net is None and "foreign_shares" in r:
                    continue

                total = (_to_numeric(f_net) or 0) + (_to_numeric(it_net) or 0) + (_to_numeric(dealer_net) or 0)

                batch.append((
                    symbol,
                    trade_date,
                    f_net,
                    it_net,
                    dealer_net,
                    total,
                    availability_date,
                    dataset_name,
                    "raw",
                    "FALLBACK",
                ))

            if batch:
                await pg_conn.executemany(
                    "INSERT INTO core.institutional_flow "
                    "(symbol, trade_date, foreign_net, investment_trust_net, "
                    "dealer_net, total_net, availability_date, source, "
                    "freshness, source_role) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
                    "ON CONFLICT DO NOTHING",
                    batch,
                )
                total_inserted += len(batch)

    logger.info(
        "  ✅ Backfilled %d institutional flows → core.institutional_flow",
        total_inserted,
    )
    return total_inserted


async def backfill_stocks(pg_conn, sqlite_conn) -> int:
    """回補 tw-quant-mcp calendar → core.stocks."""
    rows = sqlite_conn.execute(
        "SELECT value FROM cache_entries WHERE dataset='calendar' "
        "ORDER BY data_date DESC"
    ).fetchall()
    logger.info("calendar: %d entries to process", len(rows))

    all_stocks = set()
    for (value_blob,) in rows:
        records = _decode_cache_value(value_blob)
        if records is None:
            continue

        for r in records:
            if not isinstance(r, dict):
                continue
            # Handle English, Chinese, and mixed field names
            code = str(r.get("code", r.get("股票代號", r.get("Symbol", ""))))
            if not code or not code.strip():
                continue
            # Skip entries where code is empty or not alphanumeric
            if not any(c.isdigit() for c in code):
                continue
            name = str(r.get("name", r.get("公司名稱", r.get("CompanyName", code))))
            market = r.get("market", "tse") or "tse"
            sector = r.get("category", r.get("sector", None))
            industry = r.get("industry", r.get("category", None))
            all_stocks.add((
                code,
                name,
                market,
                sector or "",
                industry or "",
            ))

    inserted = 0
    for symbol, name, market, sector, industry in sorted(all_stocks, key=lambda x: (x[0], x[1])):
        await pg_conn.execute(
            "INSERT INTO core.stocks (symbol, name, market, sector, industry, "
            "security_type, listed_date, active, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW(), NOW()) "
            "ON CONFLICT (symbol) DO NOTHING",
            symbol, name, market, sector, industry, "股票", None, True,
        )
        inserted += 1

    logger.info("  ✅ Backfilled %d stocks → core.stocks", inserted)
    return inserted


async def backfill_margin(pg_conn, sqlite_conn) -> int:
    """回補 tw-quant-mcp margin → core.margin_trading.

    margin entries have two types:
    - Stock-level margin data (code/name + margin_buy/sell/balance, short_buy/sell/balance)
      → insert into core.margin_trading keyed by (symbol, trade_date)
    - Market aggregate data (_date/_table fields) — skip, not stock-level
    """
    rows = sqlite_conn.execute(
        "SELECT data_date, value FROM cache_entries WHERE dataset='margin'"
    ).fetchall()
    logger.info("margin: %d entries to process", len(rows))

    inserted = 0
    skipped = 0
    for data_date, value_blob in rows:
        records = _decode_cache_value(value_blob)
        if records is None:
            logger.warning("  skip invalid margin entry for date %s", data_date)
            skipped += 1
            continue

        batch = []
        for r in records:
            if not isinstance(r, dict):
                continue
            # Stock-level data has 'code' field; market aggregates have '_table' instead
            symbol = str(r.get("code", ""))
            if not symbol:
                # Market aggregate data (_date/_table fields) — skip
                skipped += 1
                continue

            trade_date = _to_date(r.get("date", r.get("trade_date", data_date)))
            if trade_date is None:
                trade_date = _to_date(data_date)
            if trade_date is None:
                skipped += 1
                continue

            batch.append((
                symbol,
                trade_date,
                _to_numeric(r.get("margin_buy")),
                _to_numeric(r.get("margin_sell")),
                _to_numeric(r.get("margin_balance")),
                _to_numeric(r.get("margin_limit")),
                _to_numeric(r.get("short_buy")),
                _to_numeric(r.get("short_sell")),
                _to_numeric(r.get("short_balance")),
                _to_numeric(r.get("short_limit")),
                _to_numeric(r.get("offset")),
                "tw-quant-mcp",
                trade_date,
                "raw",
                "FALLBACK",
            ))

        if batch:
            await pg_conn.executemany(
                "INSERT INTO core.margin_trading "
                "(symbol, trade_date, margin_buy, margin_sell, margin_balance, "
                "margin_limit, short_buy, short_sell, short_balance, short_limit, "
                "\"offset\", source, data_date, freshness, source_role) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) "
                "ON CONFLICT DO NOTHING",
                batch,
            )
            inserted += len(batch)

    logger.info("  ✅ Backfilled %d margin → core.margin_trading (skipped: %d)", inserted, skipped)
    return inserted


async def backfill_low_priority(sqlite_conn) -> int:

    """Count low priority datasets (not backfilled, logged for review)."""
    low_priority = ["esg", "taifex_history", "valuation", "warrants", "ex_div_calendar"]
    total = 0
    for dataset in low_priority:
        count = sqlite_conn.execute(
            f"SELECT COUNT(*) FROM cache_entries WHERE dataset='{dataset}'"
        ).fetchone()[0]
        if count > 0:
            logger.info(
                "  %s: %d entries (low priority, not auto-backfilled)", dataset, count
            )
        total += count
    return total


async def verify_backfill(pg_conn):
    """驗證回補結果。"""
    logger.info("=== MCP Backfill Verification ===")
    tables = [
        "core.stocks",
        "core.daily_prices",
        "core.financials",
        "core.monthly_revenues",
        "core.dividends",
        "core.institutional_flow",
        "core.margin_trading",
    ]
    for tbl in tables:
        count = await pg_conn.fetchval(f"SELECT COUNT(*) FROM {tbl}")
        # core.stocks has no source_role column (not a lineage table)
        if tbl == "core.stocks":
            logger.info("  %s: %d total", tbl, count)
        else:
            fallback_count = await pg_conn.fetchval(
                f"SELECT COUNT(*) FROM {tbl} WHERE source_role = 'FALLBACK'"
            )
            logger.info("  %s: %d total, %d FALLBACK", tbl, count, fallback_count)


async def main():
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://localhost:5432/twquant_shared"
    )
    mcp_cache_db = os.environ.get("MCP_CACHE_DB", "")
    dry_run = "--dry-run" in sys.argv

    if not mcp_cache_db:
        mcp_cache_db = str(
            Path.home() / "Projects" / "tw-quant-mcp" / "data" / "cache.db"
        )

    if not Path(mcp_cache_db).exists():
        logger.error("MCP cache.db not found: %s", mcp_cache_db)
        sys.exit(1)

    logger.info("MCP cache.db: %s", mcp_cache_db)

    conn = await asyncpg.connect(database_url)
    sqlite_conn = sqlite3.connect(mcp_cache_db)

    if dry_run:
        logger.info("[DRY RUN] Would backfill:")
        for ds in [
            "financials", "daily_kline", "calendar", "dividend",
            "monthly_revenue", "institutional", "foreign_holding", "margin",
        ]:
            count = sqlite_conn.execute(
                f"SELECT COUNT(*) FROM cache_entries WHERE dataset='{ds}'"
            ).fetchone()[0]
            logger.info("  %s: %d entries", ds, count)
        await conn.close()
        sqlite_conn.close()
        return

    total = 0
    total += await backfill_financials(conn, sqlite_conn)
    total += await backfill_daily_kline(conn, sqlite_conn)
    total += await backfill_dividends(conn, sqlite_conn)
    total += await backfill_monthly_revenue(conn, sqlite_conn)
    total += await backfill_institutional(conn, sqlite_conn)
    total += await backfill_stocks(conn, sqlite_conn)
    total += await backfill_margin(conn, sqlite_conn)
    await backfill_low_priority(sqlite_conn)

    logger.info("Total rows backfilled: %d", total)
    await verify_backfill(conn)
    await conn.close()
    sqlite_conn.close()
    logger.info("MCP backfill complete")


if __name__ == "__main__":
    asyncio.run(main())
