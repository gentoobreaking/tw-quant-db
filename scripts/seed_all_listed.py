#!/usr/bin/env python3
"""
seed_all_listed.py — 種子 core.stocks 全上市櫃清單

從 FinMind TaiwanStockInfo 灌入 core.stocks，供 BACKFILL_ALL_LISTED=true 全量回補。

Usage:
  DATABASE_URL=postgresql://twquant:...@localhost:5432/twquant_shared \
  FINMIND_TOKEN=xxx \
  python3 scripts/seed_all_listed.py [--dry-run] [--force]

- 預設只插入不存在的 symbol (ON CONFLICT DO NOTHING)
- --force 會更新已存在的 name/market/sector
- 無 FINMIND_TOKEN 時改用 TWSE 官方清單 fallback (少量) 或報錯

Refs: pipeline_screener.py TaiwanStockInfo, backfill.go loadStockList
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FINMIND_API = "https://api.finmindtrade.com/api/v4/data"
NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed core.stocks from FinMind TaiwanStockInfo")
    p.add_argument("--dry-run", action="store_true", help="只印出不清寫入")
    p.add_argument("--force", action="store_true", help="已存在也更新")
    p.add_argument("--limit", type=int, default=0, help="限制筆數 (0=全部)")
    return p.parse_args()


async def fetch_taiwan_stock_info(token: str) -> list[dict]:
    """呼叫 FinMind TaiwanStockInfo，取全清單。"""
    params = {"dataset": "TaiwanStockInfo", "token": token}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(FINMIND_API, params=params)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 200:
            raise RuntimeError(f"FinMind error: {body}")
        data = body.get("data", [])
        if not data:
            raise RuntimeError("FinMind TaiwanStockInfo 回傳空")
        return data


def _map_market(row: dict) -> str:
    t = str(row.get("type", "") or "").lower()
    if "tpex" in t or "otc" in t or "上櫃" in t:
        return "TPEx"
    return "TWSE"


def _map_security_type(row: dict) -> str:
    t = str(row.get("type", "") or "").lower()
    if "etf" in t:
        return "ETF"
    if "tpex" in t or "otc" in t:
        return "STOCK"
    return "STOCK"


def _to_stock_record(row: dict) -> tuple | None:
    symbol = str(row.get("stock_id") or row.get("stockId") or "").strip()
    name = str(row.get("stock_name") or row.get("stockName") or "").strip()
    if not symbol or not name:
        return None
    # 過濾指數/產業類別等非個股（stock_id 為純英文如 Plastics）
    if symbol.isalpha() and len(symbol) > 3:
        return None
    # 過濾 industry_category 為 Index 的指數
    if str(row.get("industry_category") or "").strip() == "Index":
        return None
    market = _map_market(row)
    sector = str(row.get("industry_category") or row.get("industryCategory") or "").strip() or None
    industry = sector
    sec_type = _map_security_type(row)
    return (symbol, name, market, sector, industry, sec_type, NOW, NOW)


async def seed(dry_run: bool = False, force: bool = False, limit: int = 0) -> int:
    token = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_KEY") or ""
    if not token:
        # 也嘗試從 .env 讀
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("FINMIND_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    if not token:
        logger.error("FINMIND_TOKEN 未設定，無法取全清單。請 export FINMIND_TOKEN=xxx")
        sys.exit(1)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL 未設定")
        sys.exit(1)

    logger.info("Fetching TaiwanStockInfo from FinMind...")
    rows = await fetch_taiwan_stock_info(token)
    logger.info("Fetched %d rows from FinMind", len(rows))

    records = []
    for r in rows:
        rec = _to_stock_record(r)
        if rec:
            records.append(rec)

    if limit and len(records) > limit:
        records = records[:limit]
        logger.info("Limit %d rows", limit)

    logger.info("Mapped %d stocks (sample: %s)", len(records), records[:3])

    if dry_run:
        logger.info("[DRY RUN] 不寫入，僅顯示前 10 筆")
        for rec in records[:10]:
            print(rec)
        return len(records)

    # 寫入 DB
    is_sqlite = dsn.startswith("sqlite") or os.environ.get("TW_QUANT_DB_PATH")
    if is_sqlite:
        import sqlite3

        path = os.environ.get("TW_QUANT_DB_PATH") or dsn.replace("sqlite://", "")
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS core_stocks (
                symbol VARCHAR(10) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                market VARCHAR(20) NOT NULL,
                sector VARCHAR(50),
                active BOOLEAN DEFAULT 1,
                needs_manual_review BOOLEAN DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        inserted = 0
        for rec in records:
            symbol, name, market, sector, industry, sec_type, created, updated = rec
            try:
                if force:
                    cur.execute("""
                        INSERT INTO core_stocks (symbol, name, market, sector, active)
                        VALUES (?, ?, ?, ?, 1)
                        ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, market=excluded.market, sector=excluded.sector
                    """, (symbol, name, market, sector))
                else:
                    cur.execute("""
                        INSERT OR IGNORE INTO core_stocks (symbol, name, market, sector, active)
                        VALUES (?, ?, ?, ?, 1)
                    """, (symbol, name, market, sector))
                inserted += cur.rowcount
            except Exception as e:
                logger.warning("insert %s failed: %s", symbol, e)
        conn.commit()
        conn.close()
        logger.info("✅ Inserted/updated %d / %d stocks (sqlite)", inserted, len(records))
        return inserted
    else:
        import psycopg

        # psycopg 3 — 逐筆交易，避免單筆失敗 abort 整個批次
        async_conn = await psycopg.AsyncConnection.connect(dsn)
        async with async_conn:
            inserted = 0
            for rec in records:
                symbol, name, market, sector, industry, sec_type, created, updated = rec
                try:
                    async with async_conn.transaction():
                        async with async_conn.cursor() as cur:
                            if force:
                                await cur.execute("""
                                    INSERT INTO core.stocks (symbol, name, market, sector, industry, security_type, created_at, updated_at, active)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                                    ON CONFLICT (symbol) DO UPDATE SET
                                      name = EXCLUDED.name,
                                      market = EXCLUDED.market,
                                      sector = EXCLUDED.sector,
                                      industry = EXCLUDED.industry,
                                      security_type = EXCLUDED.security_type,
                                      updated_at = EXCLUDED.updated_at
                                """, (symbol, name, market, sector, industry, sec_type, created, updated))
                            else:
                                await cur.execute("""
                                    INSERT INTO core.stocks (symbol, name, market, sector, industry, security_type, created_at, updated_at, active)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
                                    ON CONFLICT (symbol) DO NOTHING
                                """, (symbol, name, market, sector, industry, sec_type, created, updated))
                            inserted += cur.rowcount
                except Exception as e:
                    logger.warning("insert %s failed: %s", symbol, e)
        logger.info("✅ Inserted/updated %d / %d stocks (postgres)", inserted, len(records))
        return inserted


def main() -> int:
    args = _parse_args()
    asyncio.run(seed(dry_run=args.dry_run, force=args.force, limit=args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
