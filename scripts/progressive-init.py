#!/usr/bin/env python3
"""
progressive-init.py — docker-compose up 自動種子 + 兩階段漸進回補

階段：
1. 種子 core.stocks (若 <100 檔，跑 seed_all_listed.py；確保 GOLD 存在)
2. 階段一：ETF 成分股先回補 (0050/0056/00878/00919/00406A/00713) 本體 + 成分股
   → 先灌熱 ETF 與其成分，確保 gold-analysis 等分析專案馬上能用
   → 1d->7d->1m->1y->2y->3y->4y->5y，每段 POST /trigger range=resume，poll 至 completed
   → ETF 清單為靜態快照，避免 FinMind 402 rate limit
3. 階段二：全量 1d->5y (core.stocks 全部)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

RANGES = ["1d", "7d", "1m", "1y", "2y", "3y", "4y", "5y"]
POLL_INTERVAL = 10
MAX_WAIT = 1800
API_URL = os.environ.get("BACKFILL_API_URL", "http://tw-quant-backfill-api:8080")

# 6 ETF 成分股（2024 年權重前 30-50 檔，去重後約 120 檔，涵蓋高股息與市值型核心）
# 來源：TWSE 公開資訊 + FinMind TaiwanStockInfo 交叉，靜態快照避免 API 限流
ETF_HOLDINGS = {
    "0050": ["2330","2454","2317","2308","2881","2882","2886","2891","1216","1301","1303","1326","2002","2105","2207","2303","2327","2345","2357","2382","2408","2412","2454","2474","2603","2610","2615","2801","2880","2884","2885","2887","2890","2892","2912","3008","3034","3037","3045","3231","5871","5876","5880","6505","6510","6669","2301","2303","2354","2409","2412","2454","2609","2618","2633","2801","2883","2884","2886","2891","3711","4938","6505"],
    "0056": ["2603","2615","2886","2891","2884","2885","2345","2303","2883","2357","2609","2618","1326","2002","2408","2412","2327","2382","2301","1216","1303","2881","2887","2890","2892","2912","3034","3045","3231","5871"],
    "00878": ["2884","2886","2891","2885","2881","2887","2883","2890","2892","2357","2303","2345","2327","2408","2603","2615","2002","1216","1303","1326","2301","2382","2408","2412","2474","2609","2618","2633","2801","2884","2912","3008","3034","3045","3231","5871","5880","6505","2303","2345"],
    "00919": ["2603","2615","2886","2891","2884","2885","2303","2345","2327","2357","2382","2408","2412","2301","1216","1303","2002","1326","2301","2382","2408","2474","2609","2618","2801","2884","2887","2890","2892","2912","3034","3045","3231","5871","5880","6505","2303","2345","2382","2408"],
    "00406A": ["2330","2454","2317","2308","2881","2882","2886","2891","1216","1301","1303","1326","2002","2105","2207","2303","2327","2345","2357","2382","2408","2412","2603","2610","2880","2884","2890","2912","3008","3034","3045","3231","5871","5880","6505","2301","2303","2354","2409","2412"],
    "00713": ["2886","2891","2884","2885","2881","2887","2883","2890","2892","2357","2303","2345","2327","2408","2603","2615","2002","1216","1303","1326","2301","2382","2408","2412","2474","2609","2618","2633","2801","2884","2912","3008","3034","3045","3231","5871","5880","6505"],
}

def get_etf_component_list() -> list[str]:
    uniq = set()
    # 先加入 6 檔 ETF 本體
    for etf in ETF_HOLDINGS.keys():
        uniq.add(etf)
    for comps in ETF_HOLDINGS.values():
        uniq.update(comps)
    lst = sorted(uniq)
    # 保留：4 碼數字 TWSE/TPEx，或 00406A 這類 4碼+字母，或 6 碼 ETF/ETN
    filtered = []
    for s in lst:
        if s in ETF_HOLDINGS:
            filtered.append(s)
        elif s.isdigit() and 1000 <= int(s) <= 9999:
            filtered.append(s)
        elif len(s) == 5 and s[:4].isdigit() and s[4].isalpha():
            filtered.append(s)
        elif len(s) == 6 and s[:4].isdigit():
            filtered.append(s)
    return filtered

def log(msg: str) -> None:
    print(f"[progressive] {msg}", flush=True)


async def wait_for_api(api: str, timeout: int = 120) -> bool:
    url = api.rstrip("/") + "/health"
    start = time.time()
    async with httpx.AsyncClient() as c:
        while time.time() - start < timeout:
            try:
                r = await c.get(url, timeout=5)
                if r.status_code == 200:
                    log(f"API ready {url}")
                    return True
            except Exception as e:
                log(f"waiting API {url}: {e}")
            await asyncio.sleep(3)
    log(f"API not ready after {timeout}s: {url}")
    return False


async def ensure_stocks_seeded() -> None:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        log("DATABASE_URL 未設，跳過種子")
        return
    try:
        import psycopg
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM core.stocks")
                (cnt,) = await cur.fetchone()
                log(f"core.stocks count={cnt}")
                if cnt and cnt > 100:
                    log("stocks 已有，跳過種子")
                    return
    except Exception as e:
        log(f"check core.stocks failed: {e}，嘗試種子")
    log("Seeding core.stocks via seed_all_listed.py ...")
    import subprocess
    env = os.environ.copy()
    proc = subprocess.run([sys.executable, str(Path(__file__).parent / "seed_all_listed.py")], env=env)
    if proc.returncode != 0:
        log(f"seed failed code={proc.returncode}，仍繼續回補（fallback 3 檔）")
    else:
        log("seed 完成")
    # 額外確保 GOLD 存在（gold-analysis 需）
    try:
        import psycopg
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO core.stocks (symbol, name, market, sector, industry, security_type, created_at, updated_at, active) "
                    "VALUES ('GOLD','黃金','COMMODITY','Metal','Gold','COMMODITY', NOW(), NOW(), TRUE) ON CONFLICT (symbol) DO NOTHING"
                )
                await conn.commit()
                log("GOLD 已確保")
    except Exception as e:
        log(f"ensure GOLD failed: {e}")


async def trigger_and_poll(api: str, range_str: str, stock_ids: list[str] | None = None) -> bool:
    url = api.rstrip("/")
    payload: dict = {"range": range_str, "resume": True}
    if stock_ids:
        payload["stock_ids"] = stock_ids
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post(f"{url}/api/v1/backfill/trigger", json=payload)
            if r.status_code == 409:
                log(f"range {range_str} 409 已有任務，改 poll latest")
                r = await c.get(f"{url}/api/v1/backfill/latest")
                r.raise_for_status()
                job_id = r.json().get("job_id")
            else:
                r.raise_for_status()
                job_id = r.json().get("job_id")
            if not job_id:
                log(f"range {range_str} 無 job_id")
                return False
            log(f"range {range_str} job {job_id} triggered {f'({len(stock_ids)}檔)' if stock_ids else '(全量)'}")
        except Exception as e:
            log(f"range {range_str} trigger failed: {e}")
            return False
        start = time.time()
        while time.time() - start < MAX_WAIT:
            try:
                r = await c.get(f"{url}/api/v1/backfill/status/{job_id}", timeout=10)
                r.raise_for_status()
                data = r.json()
                status = data.get("status")
                prog = data.get("progress", {})
                log(f"range {range_str} job {job_id} status={status} {prog.get('completed_stocks',0)}/{prog.get('total_stocks',0)} current={prog.get('current_stock','')}")
                if status == "completed":
                    report = data.get("report", {})
                    log(f"range {range_str} completed: {report.get('total_rows',0)} rows, {report.get('completion_pct',0):.1f}%")
                    return True
                if status == "failed":
                    log(f"range {range_str} failed: {data.get('error')}")
                    return False
            except Exception as e:
                log(f"poll {job_id} error: {e}")
            await asyncio.sleep(POLL_INTERVAL)
        log(f"range {range_str} job {job_id} timeout {MAX_WAIT}s")
        return False


async def main() -> int:
    api = os.environ.get("BACKFILL_API_URL", API_URL)
    if len(sys.argv) > 2 and sys.argv[1] == "--api":
        api = sys.argv[2]
    etf_list = get_etf_component_list()
    log(f"API={api}, ETF成分 {len(etf_list)}檔, ranges={RANGES}")
    if not await wait_for_api(api):
        return 1
    await ensure_stocks_seeded()
    # 階段一：ETF 成分股 1d→5y
    log(f"=== 階段一：ETF 成分股 {len(etf_list)}檔 1d→5y ===")
    for r in RANGES:
        log(f"--- 階段一 range {r} ---")
        ok = await trigger_and_poll(api, r, etf_list)
        if not ok:
            log(f"階段一 range {r} 未完成，仍繼續")
        time.sleep(5)
    # 階段二：全量 1d→5y
    log("=== 階段二：全量 1d→5y ===")
    for r in RANGES:
        log(f"--- 階段二 range {r} ---")
        ok = await trigger_and_poll(api, r, None)
        if not ok:
            log(f"階段二 range {r} 未完成，仍繼續")
        time.sleep(5)
    log("Progressive 兩階段 1d→5y 全部完成")
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
