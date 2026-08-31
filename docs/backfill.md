# 回補使用手冊（Backfill Manual）

> `core.daily_prices` 歷史日 K 回補：透過 MCP 多源 fallback 自動補齊缺口，寫入共享 PostgreSQL `twquant_shared`。

## 1. 架構

```
TWSE WEB (tw-quant-mcp) ─┐
                         ├─► local-mcp (container, 最高權重 1.0)
TWSE MCP (remote) ───────┤──► twse-mcp (0.9, get_stock_history)
FinMind (stdio) ─────────┤──► finmind-mcp (0.7, TaiwanStockPrice)
YFinance (stdio) ────────┘──► yfinance-mcp (0.5, get_price_history → 2330.TW)

                    fallback chain 依序嘗試，直到 coverage ≥ 0.7
                              │
                              ▼
              PostgreSQL core.daily_prices (ON CONFLICT DO UPDATE)
              core.trading_calendar 判斷交易日，僅補交易日缺口
```

- **單一真相**：`core.daily_prices` 僅由回補寫入 `FALLBACK`，日常 pipeline 寫 `CANONICAL`
- **Idempotent**：重跑不產生重複列
- **批次**：5 天一批，批間隨機 2–5 秒，單源重試 60s/120s 指數退避

## 2. 前置

```bash
# 共享網路（兩 Compose 共用）
docker network create tw-quant-network  # 已存在則忽略

# 啟動依賴
cd ~/Projects/tw-quant-mcp && docker compose up -d   # tw-quant-mcp:8000 (streamable-http)
cd ~/Projects/tw-quant-db  && docker compose up -d   # postgres:5432 + pgAdmin:5050

# 環境變數（.env 自動載入，已含範例）
cat .env
# POSTGRES_PASSWORD=twquant-secret-password
# FINMIND_TOKEN=eyJ0eXAiOiJK...

# 若 FINMIND_TOKEN 有更新，重寫 .env
echo "POSTGRES_PASSWORD=$(cat secrets/postgres_password.txt)" > .env
echo "FINMIND_TOKEN=$(cat ~/.finmind_token)" >> .env
```

## 3. 快速開始

```bash
# 預設：全市場、近 7 天、實寫（已改為非 dry-run）
docker compose run --rm backfill
# 等價於
docker compose run --rm backfill --range 7d   # BACKFILL_ALL_LISTED=true

# 常用覆蓋
docker compose run --rm backfill --stock 2330 --dry-run --range 1mo          # 單檔 1 個月試跑
docker compose run --rm backfill --stock-ids "2330,0050,2317" --start 2025-08-25 --end 2026-08-28 --sources mcp
docker compose run --rm backfill --range 1Y --sources mcp                    # 近 1 年
docker compose run --rm backfill --range 5Y --sources mcp --resume           # 斷點續跑
```

`docker run` 等價（需手動帶網路與 env）：
```bash
docker run --rm --network tw-quant-network \
  -e DATABASE_URL="postgresql://twquant:$(cat secrets/postgres_password.txt)@postgres:5432/twquant_shared?sslmode=disable" \
  -e MCP_HOST="http://tw-quant-mcp:8000" \
  -e FINMIND_TOKEN="$(cat ~/.finmind_token)" \
  tw-quant-db-backfill --range 7d
```

## 4. CLI 參數

| Flag | 說明 | 預設 |
|------|------|------|
| `--range` | `5Y` / `1Y` / `3M` / `1mo` / `7d` / `1w`（支援 y/Y/m/M/w/W/d/D） | `5Y` |
| `--start` / `--end` | `YYYY-MM-DD` 明確區間（覆蓋 `--range`） | - |
| `--stock` | 單檔覆蓋（如 `2330`） | - |
| `--stock-ids` | 多檔逗號分隔（如 `2330,0050,2317`） | - |
| `--strategy` | `monthly` / `auto` | `monthly` |
| `--sources` | `mcp` / `http` / `both` | `both` |
| `--dry-run` | 試跑不寫 DB，僅印報告 | `false` |
| `--resume` | 從 `backfill_checkpoint.json` 續跑 | `false` |

## 5. 選股邏輯（優先順序）

1. `--stock` / `--stock-ids` CLI 參數
2. `STOCKS_FILE`（每行一檔，`#` 為註解）
3. `BACKFILL_ALL_LISTED=true` → `SELECT symbol FROM core.stocks WHERE active`
4. 預設 `["2330","0050","2317"]`

```bash
# 全市場
BACKFILL_ALL_LISTED=true docker compose run --rm backfill --range 7d
# 外部清單
echo -e "2330\n2317\n2454" > /tmp/my.stocks
STOCKS_FILE=/tmp/my.stocks docker compose run --rm backfill --range 1M
```

## 6. 驗證

```bash
# 回補報告（stderr JSON）
docker compose run --rm backfill --stock 2330 --start 2025-08-25 --end 2026-08-28 2>&1 | tail -5
# {"start_date":"2025-08-25","end_date":"2026-08-28","total_stocks":1,"total_rows":25,"completion_pct":83.3}

# DB 核對
docker exec twquant-shared-postgres psql -U twquant -d twquant_shared -c "
SELECT symbol, trade_date, close, source, source_role
FROM core.daily_prices WHERE symbol='2330' AND trade_date BETWEEN '2025-08-25' AND '2025-08-28'
ORDER BY trade_date;
"
# 應見 source=backfill_go / tw-quant-mcp，source_role=FALLBACK
```

## 7. 除錯

| 現象 | 原因 | 解法 |
|------|------|------|
| `lookup postgres: no such host` | 未接 `tw-quant-network` | `docker network connect tw-quant-network <container>` 或重建 `docker compose up -d` |
| `password authentication failed` | `POSTGRES_PASSWORD` 未帶 | 確認 `.env` 有值或 `export POSTGRES_PASSWORD=$(cat secrets/postgres_password.txt)` |
| `tls error` | pgx 預設 TLS | `DATABASE_URL` 已加 `?sslmode=disable` |
| `mcp ping failed` / `client not initialized` | MCP 未就緒 | 確認 `tw-quant-mcp` 健康：`curl http://localhost:8000/health` |
| `Unknown tool: get_stock_daily_kline` | 舊版 image | `docker compose build backfill` 重建 |
| `needs_manual_review does not exist` | 舊 DB 缺欄 | `ALTER TABLE core.stocks ADD COLUMN needs_manual_review BOOLEAN DEFAULT FALSE`（已修） |
| `checkpoint permission denied` | `/app` 權限 | 已修 `Dockerfile: chown app:app /app`，重建 image |

## 8. 排程建議

```bash
# 每日 17:00 補近 7 天（cron）
0 17 * * * cd ~/Projects/tw-quant-db && docker compose --profile backfill run --rm backfill --range 7d >> ~/logs/backfill.log 2>&1
```

## 9. 檔案

- `backfill/backfill.go`：缺口偵測、批次、fallback、checkpoint
- `backfill/sources.go`：四源 MCP client（local/twse/finmind/yfinance）與文字表解析
- `Dockerfile.backfill`：Go 二進位 + `uvx finmind-mcp/yfinance-mcp`（yfinance 需 `mcp<2`）
- `core/schema.sql`：`core.trading_calendar`、`core.daily_prices` 等
