# tw-quant-db

Shared PostgreSQL schema definition and migration/backfill scripts for the tw-quant ecosystem. This repository owns the **data layer** — the `core`, `pickup`, `selector`, `signal`, and `audit` schemas — plus scripts that migrate data into `core` and verify integrity. It does **not** implement data collection pipelines; those live in [`tw-quant-pickup`](https://github.com/gentoobreaking/tw-quant) (the pickup pipeline) and [`tw-quant-mcp`](https://github.com/gentoobreaking/tw-quant) (TwseOpenAPI / FinMind fetchers).

> **一鍵起**：`docker compose up -d` 即自動完成 5 schemas 建置 → `core.stocks` 種子（3114 檔）→ 漸進回補 `1d→7d→1m→1y→2y→3y→4y→5y` → pgAdmin 就緒。詳見 [自動化](#自動化auto-seed--漸進回補) 與 [Quick Start](#quick-start一鍵起)。

## Overview

Tw-quant is a Taiwanese stock market analysis system. Multiple projects (pickup, selector, signal, daybrain, mcp) previously each maintained their own SQLite databases with overlapping but incompatible schemas. This repository centralizes the **shared PostgreSQL data layer**:

- **core schema**: raw/fact tables — single source of truth, written only by the pickup pipeline
- **pickup schema**: tw-quant-pickup business logic tables (factor_scores, valuations, rankings, audit logs)
- **selector schema**: tw-quant-selector tables (portfolio, backtest, alerts)
- **signal schema**: tw-quant-signal technical indicator tables
- **audit schema**: shared audit tables (operation_logs)

## Features

- PostgreSQL schema definitions for 5 schemas (`core/`, `pickup/`, `signal/`, `init-scripts/`)
- **自動種子 + 漸進回補**（`tw-quant-init` + `tw-quant-backfill-api`）：`docker compose up` 自動灌 `core.stocks` 3114 檔（FinMind TaiwanStockInfo）並依 `1d→7d→1m→1y→2y→3y→4y→5y` 漸進呼叫 backfill API，缺口檢查走 `core.trading_calendar`，`ON CONFLICT DO NOTHING/UPDATE` 保 idempotent（見 [自動化](#自動化auto-seed--漸進回補)）
- **Go backfill service** (`backfill/`): MCP 多源 fallback（local-mcp → twse-mcp → finmind-mcp → yfinance-mcp）自動補齊 `core.daily_prices` 缺口，支援 7d/1mo/5Y、斷點續跑、交易日曆判斷（詳見 [docs/backfill.md](docs/backfill.md)）
- Migration script (`scripts/migrate_to_core.py`): copies existing pickup data → core schema
- MCP cache backfill (`scripts/backfill_from_mcp.py`): bulk-imports 4,818 cache entries from tw-quant-mcp's `cache.db` into `core.*` with `source_role='FALLBACK'`
- Signal backfill (`scripts/backfill_from_signal.py`): imports data from tw-quant-signal SQLite → core
- Signal-to-PostgreSQL migration (`scripts/migrate_signal_to_pg.py`)
- Compatibility views for projects with different column naming conventions (`symbol` vs `stock_id`)
- Idempotent schema creation with `CREATE TABLE IF NOT EXISTS` and `DO $$ ... END $$` constraint guards
- 共享 `tw-quant-network`（`external: true`）與 `tw-quant-mcp` 共用；`docker compose up -d` 啟動 postgres + backfill-api + progressive init + pgAdmin（**8001**）

## Architecture

```
tw-quant-db/                    ← this repo (schema + scripts)
├── core/schema.sql             ← core.* fact tables + indexes + constraints (+ trading_calendar)
├── pickup/schema.sql           ← pickup.* business tables
├── backfill/                   ← Go 回補服務（MCP fallback chain + HTTP API :8080）
├── migrations/                 ← incremental SQL migrations
├── init-scripts/               ← Docker entrypoint init (schema creation)
├── scripts/                    ← Python migration & backfill scripts
│   ├── seed_all_listed.py      ← FinMind TaiwanStockInfo → core.stocks (3114 檔)
│   └── progressive-init.py     ← 自動種子 + 漸進 1d→5y（呼叫 backfill-api）
└── docker-compose.yml          ← shared PostgreSQL + backfill-api + init + pgAdmin(8001)

tw-quant-mcp/                   ← MCP fetchers (separate repo, shared tw-quant-network)
  data/cache.db                 ← 835MB SQLite cache (4,818 entries, 13 datasets)
  cmd/mcp-server                ← streamable-http :8000, 252 tools
tw-quant-pickup/                ← pickup pipeline (separate repo)
  collectors/                   ← writes core.* tables (CANONICAL)
  api/                          ← FastAPI with search_path=core,pickup

  common/cache.py               ← DiskCache with PostgreSQL/PostgreSQL dual backend
  common/factors.py             ← factor computation
  pipeline_screener.py          ← pipeline entry point
```

### 共享 DB 依賴圖

```mermaid
flowchart TB
    net["tw-quant-network (external: true)"]

    subgraph db_compose["tw-quant-db / docker-compose.yml"]
        pg["tw-quant-db<br/>postgres:16-alpine :5432<br/>twquant_shared<br/>init-scripts/*"]
        api["tw-quant-backfill-api :8080<br/>Go backfill --mode=server<br/>MCP fallback chain"]
        init["tw-quant-init (one-shot)<br/>python:3.12-alpine<br/>seed 3114 + 1d→5y progressive"]
        pgadmin["tw-quant-pgadmin :8001→80<br/>admin@twquant.internal"]
        manual["tw-quant-backfill (profile: manual)<br/>one-shot CLI --range 7d"]
    end

    mcp["tw-quant-mcp<br/>:8888 streamable-http<br/>(external repo)"]

    net --- pg
    net --- api
    net --- init
    net --- pgadmin
    net --- manual
    net --- mcp

    pg -->|healthcheck| api
    pg -->|healthcheck| init
    api -->|healthcheck| init
    init -->|1. seed core.stocks via FinMind| pg
    init -->|2. POST /api/v1/backfill/trigger<br/>1d,7d,1m,1y,2y,3y,4y,5y --resume| api
    api -->|getMissingDates via trading_calendar<br/>ON CONFLICT idempotent| pg
    api -.->|MCP fallback| mcp
    manual -.->|手動觸發，不隨 up 啟動| pg
    manual -.->|MCP fallback| mcp

    pickup["tw-quant-pickup<br/>CANONICAL writer"]
    selector["tw-quant-selector<br/>read-only"]
    signal["tw-quant-signal<br/>read-only"]
    pickup -->|CANONICAL writes| pg
    selector -->|SELECT| pg
    signal -->|SELECT| pg
```

```
# 文字版依賴順序（docker compose up 啟動順序）
tw-quant-db (healthy) ─┬─► tw-quant-backfill-api (healthy) ─┬─► tw-quant-init (自動種子+漸進)
                       └─► tw-quant-pgadmin (8001)           │
                                                            └─► core.stocks + core.daily_prices
tw-quant-mcp (外部，需先 up) ──► 提供 MCP 數據源給 backfill-api
tw-quant-backfill (manual profile) ──► 僅手動觸發，不隨 up 啟動
```

> **網路前提**：`tw-quant-network` 為 `external: true`，需先 `docker network create tw-quant-network`（若已存在忽略）。`tw-quant-mcp` 與本專案共用此網路，backfill 才能透過 `http://tw-quant-mcp:8888` 取數。

### Data Lineage Model

Every row in `core.*` tables includes three lineage columns:
- `source`: which system wrote the row (e.g., `"tw-quant-mcp"`, `"tw-quant-pickup"`)
- `data_date`: when the underlying data was valid
- `freshness`: `"raw"` (from source) or `"processed"` (computed)
- `source_role`: `"CANONICAL"` (pickup pipeline, primary), `"SEMI_OFFICIAL_REALTIME"` (real-time), or `"FALLBACK"` (backfilled from cache)

All `core.*` tables have a `CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'))` constraint.

## 自動化（Auto Seed + 漸進回補）

`docker compose up -d` 會自動觸發 **tw-quant-init** 完成「種子 + 漸進回補」全流程，無需手動介入。設計目標：**可重跑（idempotent）、可續跑（--resume）、尊重交易日**。

### 流程

```
docker compose up -d
  │
  ├─► tw-quant-db            健康檢查 (pg_isready)
  ├─► tw-quant-backfill-api  健康檢查 (GET /health)  ← Go 二進位 --mode=server :8080
  └─► tw-quant-init          等待上述皆 healthy 後執行：
        1. ensure_stocks_seeded()
           SELECT count(*) FROM core.stocks
           ├─ >100  → 跳過（已有）
           └─ ≤100/空 → python seed_all_listed.py
                        FinMind TaiwanStockInfo → 3114 檔 → core.stocks
                        （--force 可覆寫；含 market/security_type 對映、ON CONFLICT 處理）
        2. 依序 POST /api/v1/backfill/trigger 對 backfill-api 做漸進：
           RANGES = ["1d","7d","1m","1y","2y","3y","4y","5y"]
           每段 payload: {"range": "<r>", "resume": true}
           → 取得 job_id → poll GET /api/v1/backfill/status/{job_id} 直到 completed/failed
           → 間隔 5s（避免 FinMind 600 req/hr 限流）
           → 409 已有任務 → 改 poll /latest
           → 超時 30m/段（MAX_WAIT=1800s），失敗仍續下一段
```
### 兩階段漸進回補（ETF 先 → 全量）

`progressive-init.py` 採 **兩階段** 執行，確保 gold-analysis 等分析專案能馬上用熱 ETF 與其成分股：

1. **階段一 — ETF 成分股** (0050/0056/00878/00919/00406A/00713) 本體 + 成分股
   - ETF 清單為靜態快照（`ETF_HOLDINGS` dict in `scripts/progressive-init.py`），來源 TWSE 公開資訊 + FinMind 交叉
   - 避開 FinMind 402 限流（不用動態呼叫 `TaiwanStockHoldingShares`）
   - 1d→7d→1m→1y→2y→3y→4y→5y，每段 `--resume`，poll 至 `completed`
2. **階段二 — 全量** (core.stocks 全部 3114 檔)
   - 1d→7d→1m→1y→2y→3y→4y→5y，每段 `--resume`
   - `progressive-init` 透過 `BACKFILL_API_URL` 呼叫 `tw-quant-backfill-api` 的 HTTP API，而**非**容器內 CLI
   - 缺口由 backfill-api 內建 `getMissingDates(trading_calendar)` 檢查，僅回補交易日缺缺的日期
   - 每段間隔 5s 避 FinMind 600 req/hr 限流；409 轉 poll `/latest`

GOLD 資料回補：gold-analysis backend `core.daily_prices` 讀取 `symbol='GOLD'`，由 `backfill_gold_yfinance.py` 灌入（yfinance `GC=F`），獨立於漸進回補階段。

### 缺口檢查（trading_calendar）

Go 服務 `getMissingDates()` 針對每檔 `symbol`、區間 `[start,end]` 執行：

```sql
WITH RECURSIVE date_series(d) AS (
  VALUES ($1::date) UNION ALL SELECT (d + INTERVAL '1 day')::date FROM date_series WHERE d < $2::date
)
SELECT ds.d
FROM date_series ds
LEFT JOIN core.daily_prices dp ON dp.symbol=$3 AND dp.trade_date=ds.d
LEFT JOIN core.trading_calendar tc ON tc.trade_date=ds.d
WHERE dp.trade_date IS NULL
  AND COALESCE(tc.is_trading, EXTRACT(DOW FROM ds.d) NOT IN (0,6)) = TRUE
```

- 有 `core.trading_calendar` 則以其 `is_trading` 為準；無則 fallback 週末排除（六日非交易日）。
- 僅回補「交易日且缺資料」的日期，不補假日；`coverage ≥ 0.7` 才寫入。

### Idempotent 保證

- **core.stocks**：`seed_all_listed.py` 使用 `INSERT ... ON CONFLICT (symbol) DO UPDATE/DO NOTHING`，重跑不重複，已有 3114 檔則 `ensure_stocks_seeded` 直接跳過。
- **core.daily_prices**：`upsertPrices` 採 `ON CONFLICT (symbol, trade_date) DO NOTHING`（FALLBACK）或 `DO UPDATE WHERE source_role='FALLBACK'`（CANONICAL 可升級 FALLBACK），重跑不產生重複列；`--resume` 從 `backfill_checkpoint.json` 續跑，已完成區間自動跳過。
- **progressive-init** 本身可重放：`docker compose up -d` 重啟或 `docker compose restart tw-quant-init` 皆安全；`tw-quant-init` 為 `restart: "no"` 一次性容器，失敗不循環重試，需手動 `docker compose run --rm tw-quant-init` 或重建。

### 服務對照：tw-quant-init vs tw-quant-backfill

| 服務 | 容器 | 觸發時機 | 用途 | Profile |
|---|---|---|---|---|
| **tw-quant-init** | `tw-quant-init` | `docker compose up -d` 自動（depends_on: db healthy + api healthy） | 自動種子 `core.stocks` + 漸進 `1d→5y`（透過 `BACKFILL_API_URL=http://tw-quant-backfill-api:8080` 呼叫） | 預設啟用（無 profile） |
| **tw-quant-backfill-api** | `tw-quant-backfill-api` | `docker compose up -d` 自動（`--mode=server --port=8080`，`restart: unless-stopped`） | 常駐 HTTP API 供 init/排程呼叫；實際執行 Go backfill 邏輯 | 預設啟用 |
| **tw-quant-backfill** | `tw-quant-backfill` | **手動** `docker compose --profile manual run --rm tw-quant-backfill ...` | 單次 CLI 回補（`--range 7d/1m/1y/5y`、`--stock 2330`、`--dry-run`、`--resume` 等），與 `tw-quant-mcp` 直連 | `manual` |

> **一句話**：日常 `docker compose up -d` 只會跑 `tw-quant-init`（自動）；想手動補特定區間/個股，用 `tw-quant-backfill`（manual profile）或直接打 `tw-quant-backfill-api` 的 HTTP API。

## Project Structure

```
tw-quant-db/
├── core/
│   └── schema.sql          # 8 core fact tables + indexes + constraints + trading_calendar
├── backfill/               # Go 回補服務（MCP fallback chain, docs/backfill.md）
│   ├── backfill.go         # 缺口偵測、批次、fallback、checkpoint、HTTP server
│   ├── sources.go          # local/twse/finmind/yfinance MCP clients
│   ├── types.go
│   └── go.mod
├── pickup/
│   └── schema.sql          # pickup business tables (cache, factor_scores, etc.)
├── init-scripts/
│   └── 01-create-schemas.sql  # CREATE SCHEMA for all 5 schemas
├── migrations/
│   ├── T011-signal-views.sql     # signal.* read-only views
│   ├── T017-margin-trading.sql   # core.margin_trading table
│   └── T018-pickup-cache.sql     # pickup.cache table for DiskCache
├── docs/
│   └── backfill.md         # 回補使用手冊
├── scripts/
│   ├── seed_all_listed.py        # FinMind 全市場 3114 檔 → core.stocks
│   ├── progressive-init.py       # 自動種子 + 漸進 1d→5y（呼叫 backfill-api）
│   ├── migrate_to_core.py        # pickup → core data migration
│   ├── backfill_from_mcp.py      # MCP cache.db → core (FALLBACK)
│   ├── backfill_from_signal.py   # signal SQLite → core
│   └── migrate_signal_to_pg.py   # signal SQLite → PostgreSQL
├── docker-compose.yml       # shared PostgreSQL + backfill-api + init + pgAdmin(8001)
├── Dockerfile.backfill      # Go backfill multi-stage build
├── .env.example             # 環境變數範例（複製為 .env）
└── secrets/                 # gitignored (password files)
```
## Requirements

- PostgreSQL 16 (Docker)
- Go 1.25+（backfill 服務）
- Python 3.11+ for scripts
- `asyncpg` Python package (`pip install asyncpg`)

## Installation

### 1. 準備共享網路與環境變數

```bash
docker network create tw-quant-network  # 與 tw-quant-mcp 共用（external: true），已存在則忽略

# .env 設定（擇一）
cp .env.example .env   # 範本含 POSTGRES_PASSWORD / FINMIND_TOKEN 佔位符，填入真實值
# 或從 secrets / 舊 token 生成：
echo "POSTGRES_PASSWORD=$(cat secrets/postgres_password.txt)" > .env
echo "FINMIND_TOKEN=$(cat ~/.finmind_token)" >> .env
cat .env  # 確認
# POSTGRES_PASSWORD=twquant-secret-password
# FINMIND_TOKEN=eyJ0eXAiOiJK...
```

> **提示**：`.env` 已在 `.gitignore`，勿提交。`POSTGRES_PASSWORD` 需與 `secrets/postgres_password.txt` 一致（compose 透過 `${POSTGRES_PASSWORD}` 注入，DB 則走 `POSTGRES_PASSWORD_FILE`）。`FINMIND_TOKEN` 用於 `seed_all_listed.py` 拉全市場清單，無則種子階段會跳過/失敗但仍嘗試回補（fallback 3 檔）。

### 2. 啟動共享 PostgreSQL（一鍵起，自動種子+漸進回補）

```bash
cd tw-quant-db
docker compose up -d
# 啟動：postgres:5432 + backfill-api:8080 + pgadmin:8001 + tw-quant-init(一次性)
# tw-quant-init 會：seed 3114 檔 → 漸進 1d→7d→1m→1y→2y→3y→4y→5y（每段 --resume，poll 至完成）
# 需 tw-quant-mcp 已在別專案 `docker compose up -d`（同 tw-quant-network），否則 MCP 數據源不可用但 DB/seed 仍可完成
```

This starts:
- PostgreSQL on port **5432** (user: `twquant`, database: `twquant_shared`)
- pgAdmin on port **8001** (email: `admin@twquant.internal`，密碼見 `secrets/pgadmin_password.txt`，瀏覽 `http://localhost:8001`)
- tw-quant-backfill-api on port **8080** (`GET /health` 健康檢查，`POST /api/v1/backfill/trigger` 觸發回補)
- tw-quant-init (one-shot)：自動 `seed_all_listed.py` + 漸進 `1d→5y`（`restart: "no"`，日誌 `docker logs tw-quant-init`）

Secrets are mounted from `./secrets/` (gitignored). See `secrets/` for required files. `.env` 亦可覆蓋 `POSTGRES_PASSWORD` / `FINMIND_TOKEN`。

### 3. Apply schema

The Docker entrypoint automatically runs `init-scripts/01-create-schemas.sql` on first startup, creating all 5 schemas.

For incremental migrations:

```bash
# Apply a specific migration
psql -U twquant -h localhost -d twquant_shared -f migrations/T017-margin-trading.sql

# Or create core schema + migrate pickup data
DATABASE_URL="postgresql://twquant:<password>@localhost:5432/twquant_shared" \
  python3 scripts/migrate_to_core.py
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | (無，必填) | PostgreSQL 密碼，需與 `secrets/postgres_password.txt` 一致；`tw-quant-db`/`backfill-api` 透過 `${POSTGRES_PASSWORD}` 組 `DATABASE_URL` |
| `FINMIND_TOKEN` | - | FinMind API token（FinMind → `seed_all_listed.py` 全市場種子；亦為 backfill fallback 源之一） |
| `DATABASE_URL` | `postgresql://twquant:${POSTGRES_PASSWORD}@tw-quant-db:5432/twquant_shared?sslmode=disable` | PostgreSQL connection string（compose 內自動組裝） |
| `BACKFILL_API_URL` | `http://tw-quant-backfill-api:8080` | `tw-quant-init` 呼叫 backfill-api 的位址（compose 內網） |
| `MCP_CACHE_DB` | `~/Projects/tw-quant-mcp/data/cache.db` | Path to tw-quant-mcp cache.db（僅 `backfill_from_mcp.py`） |
| `MCP_HOST` | `http://tw-quant-mcp:8888` | tw-quant-mcp streamable-http（compose 內網；舊文件 8000 已改 8888） |
| `BACKFILL_ALL_LISTED` | `true` (compose 預設) | 全市場回補（`SELECT symbol FROM core.stocks WHERE active`） |
| `STOCK_IDS` | - | 指定 `2330,0050` |
| `STOCKS_FILE` | - | 外部清單每行一檔 |

> `.env.example` 為範本，`cp .env.example .env` 後填真實值。`secrets/` 仍為 Docker secrets 掛載來源，兩者擇一或並存皆可（`.env` 供 compose 變數展開，`secrets/*.txt` 供容器內 `*_FILE` 讀取）。

### Database Connection

```
postgresql://twquant:<password>@localhost:5432/twquant_shared
```

Password is in `secrets/postgres_password.txt` (gitignored) or `.env` (`POSTGRES_PASSWORD`).

## Quick Start（一鍵起）

```bash
# 0. 前置（只需一次）
docker network create tw-quant-network
cp .env.example .env  # 填入 POSTGRES_PASSWORD / FINMIND_TOKEN
# 或：echo "POSTGRES_PASSWORD=$(cat secrets/postgres_password.txt)" > .env
#     echo "FINMIND_TOKEN=$(cat ~/.finmind_token)" >> .env

# 1. 啟動依賴（別專案，共用 tw-quant-network）
cd ~/Projects/tw-quant-mcp && docker compose up -d   # tw-quant-mcp:8888

# 2. 一鍵起 tw-quant-db（自動種子 + 漸進回補 + pgAdmin）
cd ~/Projects/tw-quant-db && docker compose up -d
# → postgres:5432 + backfill-api:8080 + pgadmin:8001 + tw-quant-init(1d→5y)
# 日誌：
docker logs -f tw-quant-init          # 看 seed + 漸進 1d→5y 進度
docker logs -f tw-quant-backfill-api  # 看 backfill 批次/fallback/checkpoint
# 驗證：
curl http://localhost:8080/health
psql "postgresql://twquant:$(cat secrets/postgres_password.txt)@localhost:5432/twquant_shared" -c "SELECT count(*) FROM core.stocks;"  # 應 3114
psql "postgresql://twquant:$(cat secrets/postgres_password.txt)@localhost:5432/twquant_shared" -c "SELECT count(*) FROM core.daily_prices;"

# 3. 手動回補（覆蓋參數，manual profile，不隨 up 自動跑）
docker compose --profile manual run --rm tw-quant-backfill --stock 2330 --dry-run --range 1mo
docker compose --profile manual run --rm tw-quant-backfill --range 1Y --sources mcp
docker compose --profile manual run --rm tw-quant-backfill --range 5Y --resume
# 或打 HTTP API（供排程/外部呼叫）：
curl -X POST http://localhost:8080/api/v1/backfill/trigger -H 'Content-Type: application/json' -d '{"range":"7d","resume":true}'
curl http://localhost:8080/api/v1/backfill/status/<job_id>
curl http://localhost:8080/api/v1/backfill/latest
# 詳見 docs/backfill.md 與 自動化章節
```

> **依賴關係**：`tw-quant-init` 依賴 `tw-quant-db:healthy` + `tw-quant-backfill-api:healthy`；`tw-quant-backfill-api` 依賴 `tw-quant-db:healthy`；`pgAdmin` 依賴 `tw-quant-db:healthy`；`tw-quant-backfill`（manual）依賴 `tw-quant-db:healthy` 但不自動啟動。外部 `tw-quant-mcp` 需先 `up` 否則回補無 MCP 數據源（但不阻擋 DB 啟動與種子）。

## Usage

### 自動種子 + 漸進回補（docker compose up 即觸發）

`tw-quant-init` + `tw-quant-backfill-api` 為預設自動化，無需手動操作：

```bash
docker compose up -d
docker logs -f tw-quant-init
# [progressive] API=http://tw-quant-backfill-api:8080, ranges=[1d 7d 1m 1y 2y 3y 4y 5y]
# [progressive] API ready http://tw-quant-backfill-api:8080/health
# [progressive] core.stocks count=0  # 或 3114
# [progressive] Seeding core.stocks via seed_all_listed.py ...
# [progressive] range 1d job <id> triggered → status=completed  ... rows, 83.3%
# [progressive] === Progressive range 7d === ...
# [progressive] Progressive 1d→5y 全部完成

# 單獨重跑 progressive（不重建 DB）
docker compose restart tw-quant-init  # restart: "no"，需用 run
docker compose run --rm tw-quant-init
# 或本機直跑
BACKFILL_API_URL=http://localhost:8080 DATABASE_URL=... python3 scripts/progressive-init.py
```

### Go 回補（MCP fallback chain → core.daily_prices）

`backfill/` 為 Go 二進位，透過 MCP 多源自動補齊缺口，`coverage ≥0.7` 才寫入，`trading_calendar` 判定交易日，`ON CONFLICT DO UPDATE` 保 idempotent。**預設由 `tw-quant-init` 漸進呼叫**，亦可手動觸發：

```bash
# 手動 CLI（manual profile，覆蓋 compose 預設 --range 7d）
docker compose --profile manual run --rm tw-quant-backfill --stock 2330 --dry-run --range 1mo
docker compose --profile manual run --rm tw-quant-backfill --stock-ids "2330,0050" --start 2025-08-25 --end 2026-08-28 --sources mcp
docker compose --profile manual run --rm tw-quant-backfill --range 1Y --sources mcp --resume

# HTTP API（tw-quant-backfill-api :8080，供 tw-quant-init 與排程）
curl -X POST http://localhost:8080/api/v1/backfill/trigger -H 'Content-Type: application/json' -d '{"range":"7d"}'
curl -X POST http://localhost:8080/api/v1/backfill/trigger -H 'Content-Type: application/json' -d '{"range":"1m","resume":true}'
curl -X POST http://localhost:8080/api/v1/backfill/trigger -H 'Content-Type: application/json' -d '{"stock":"2330","range":"1y"}'
```
詳見 [docs/backfill.md](docs/backfill.md) 與 [自動化](#自動化auto-seed--漸進回補)。

### Backfill from MCP cache

Imports data from tw-quant-mcp's `cache.db` (financials, daily_kline, dividends, monthly_revenues, institutional flows, margin trading, stocks) into `core.*` tables with `source_role='FALLBACK'`:

```bash
DATABASE_URL="postgresql://twquant:<password>@localhost:5432/twquant_shared" \
  MCP_CACHE_DB="/path/to/tw-quant-mcp/data/cache.db" \
  python3 scripts/backfill_from_mcp.py
```

Current backfill results:
| core.table | rows | source_role distribution |
|---|---|---|
| financials | 3,462 | 100% FALLBACK |
| daily_prices | 65 | 100% FALLBACK |
| dividends | 1,196 | 100% FALLBACK |
| monthly_revenues | 890 | 100% FALLBACK |
| institutional_flow | 923 | 100% FALLBACK |
| margin_trading | 1,295 | 100% FALLBACK |
| stocks | 11,211 | N/A (no source_role) |

### Migrate from pickup to core

If you have existing pickup data in PostgreSQL:

```bash
DATABASE_URL="postgresql://twquant:<password>@localhost:5432/twquant_shared" \
  python3 scripts/migrate_to_core.py
```

### Migrate from signal SQLite

```bash
DATABASE_URL="postgresql://twquant:<password>@localhost:5432/twquant_shared" \
  SIGNAL_SQLITE_DB="/path/to/tw-quant-signal/data/cache.db" \
  python3 scripts/migrate_signal_to_pg.py
```

## Data Model

### core.stocks
Company metadata. Primary key: `symbol`.

### core.daily_prices
Daily OHLCV + adjusted close. Primary key: `(symbol, trade_date)`.

### core.financials
Quarterly financial statements. Primary key: `(symbol, fiscal_year, fiscal_quarter, revision)`.

### core.monthly_revenues
Monthly revenue reports. Primary key: `(symbol, year_month)`.

### core.dividends
Annual dividend data (cash/stock dividend). Primary key: `(symbol, fiscal_year)`.

### core.institutional_flow
Foreign investment trust + dealer net flow data. Primary key: `(symbol, trade_date)`.

### core.market_context
Options/TAIFEX market context. Primary key: `(context_type, symbol, trade_date)`.

### core.margin_trading
Margin financing + short selling data from TWSE. Primary key: `(symbol, trade_date)`.

### core.universe_flags
Special status flags (attention, disposition, suspended) for stocks. Primary key: `(symbol, flag_date)`.

### core.trading_calendar
Trading day flags for gap detection (`is_trading`, `day_of_week`). Primary key: `trade_date`. Used by `getMissingDates()` to avoid filling holidays.

## Error Handling

- **Schema creation**: All `CREATE TABLE` statements use `IF NOT EXISTS`; constraint additions use `DO $$ BEGIN ... IF NOT EXISTS ... END $$` guards for idempotency.
- **Duplicate data**: Backfill scripts use `INSERT ... ON CONFLICT DO NOTHING` to avoid overwriting existing rows (preserves CANONICAL data from the pickup pipeline). Go 回補則 `ON CONFLICT DO UPDATE WHERE source_role='FALLBACK'` 允許 CANONICAL 升級 FALLBACK，FALLBACK 間可覆寫，重跑不重複。
- **Missing gaps**: `getMissingDates()` 以 `core.trading_calendar` 判定交易日，週末/假日不視為缺口；`coverage < 0.7` 則標 `needs_manual_review`。
- **Connection errors**: `backfill_from_mcp.py` catches `asyncpg` connection errors and exits with code 1. Go 服務對 MCP 單源失敗做 `60s/120s` 指數退避重試，批間 `2–5s` 隨機延遲。

## Logging and Observability

Scripts log progress to stdout via Python `logging`:
- `INFO`: dataset processing counts, backfill totals, row counts
- `WARNING`: invalid/skipped cache entries, unrecoverable keys
- `ERROR`: connection failures, missing cache.db

Example:
```
INFO:__main__:margin: 3 entries to process
INFO:__main:  ✅ Backfilled 1295 margin → core.margin_trading (skipped: 3)
INFO:__main:  core.margin_trading: 1295 total, 1295 FALLBACK
```

Go 回補與 progressive-init 日誌：
```bash
docker logs tw-quant-backfill-api   # 批次、fallback、checkpoint、coverage
docker logs tw-quant-init           # [progressive] seed 計數、每段 range job 狀態、poll 進度
cat backfill_data/backfill_report.json  # JSON 報告 total_rows / completion_pct
```

## Testing

Integration tests live in `tw-quant-pickup/tests/integration/` (separate repo). Key test areas:

- `test_migrate_postgres.py`: Schema creation, lineage columns, constraints, snapshot FK routing
- `test_pit_repository_e2e.py`: Financials PIT queries, OTC backfill immutability, institutional availability
- `test_snapshot_e2e.py`: Snapshot freeze/rerun, archive with audit
- `test_api_e2e.py`: API endpoints end-to-end

Run from the tw-quant-pickup project:
```bash
cd ~/Projects/tw-quant-pickup
DATABASE_URL="postgresql://twquant:<password>@localhost:5432/twquant_shared" \
  uv run pytest tests/integration/ -v
```

Result: **118 integration tests pass**, 3 skipped (live TEJ credentials not available).

## Build

No build step — SQL schema files and Python scripts are used directly. Go backfill 需 `docker compose build tw-quant-backfill-api` 或 `go build ./backfill`。

## Deployment

```
tw-quant-db/
├── core/                    # core.* schema (+ trading_calendar)
├── backfill/                # Go 回補：缺口偵測 + fallback + HTTP API
├── init-scripts/            # Docker entrypoint: creates all 5 schemas
├── migrations/              # Incremental migrations (T011, T017, T018)
├── scripts/                 # Python migration/backfill scripts (+ seed/progressive)
├── docker-compose.yml       # PostgreSQL 16 + backfill-api:8080 + init + pgAdmin:8001
├── Dockerfile.backfill      # Go multi-stage build
├── .env.example             # 環境變數範本（cp .env.example .env）
└── secrets/                 # gitignored password files
```

Deploy steps:
1. Clone this repo
2. `cp .env.example .env` 並填 `POSTGRES_PASSWORD` / `FINMIND_TOKEN`（或確保 `secrets/postgres_password.txt` / `~/.finmind_token` 存在）
3. `docker network create tw-quant-network`（若已有忽略）
4. `docker compose up -d`（含 `tw-quant-mcp` 先 `up -d` 提供 MCP 數據源）
5. `docker logs -f tw-quant-init` 確認 seed 3114 + 漸進 `1d→5y` 完成；`http://localhost:8001` 開 pgAdmin

手動回補（不隨 `up` 自動跑）：
```bash
docker compose --profile manual run --rm tw-quant-backfill --range 1m --dry-run
curl -X POST http://localhost:8080/api/v1/backfill/trigger -d '{"range":"7d","resume":true}' -H 'Content-Type: application/json'
```

## Limitations

- No test suite in this repository (tests live in `tw-quant-pickup`)
- `backfill_from_mcp.py` margin backfill: 2 of 3 cache entries decode to stock-level data (1,295 rows); 1 entry uses a different encoding format and is skipped
- `backfill_from_mcp.py` daily_kline: 62 of 77 cache entries are candle data (timestamp/open/high/low/close/volume) that require key reversal; some keys cannot be reversed due to missing stock code, resulting in 65 rows inserted (some candle entries produce multiple daily price records)
- `backfill_from_mcp.py` financials: 4,215 cache entries produce 32,036 records, but after `ON CONFLICT DO NOTHING` (deduplication by primary key), only 3,462 unique rows remain. The task acceptance criterion of "≥4,215 rows" was based on cache entry count, not unique DB rows
- tw-quant-signal's `common/cache.py` DiskCache PostgreSQL backend is in the `tw-quant` repo (separate project)
- `tw-quant-init` 漸進 1d→5y 依賴 `tw-quant-backfill-api` 與 `tw-quant-mcp`；若 MCP 未就緒，對應區間 `coverage` 可能不足而標 `needs_manual_review`，需事後手動 `--resume` 補齊
- 漸進 1d→5y 全量回補約需數十分鐘至數小時（受 FinMind 600 req/hr 與 MCP 限流影響），`MAX_WAIT=30m/段` 超時會進下一段，可重跑 `tw-quant-init` 續補

## Development Guide

### Schema design rules

1. All `core.*` tables must have `source`, `data_date`, `freshness`, `source_role` lineage columns
2. `source_role` must have a CHECK constraint: `IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK')`
3. Primary keys must not change once data is inserted (use `ON CONFLICT DO NOTHING` for backfills)
4. Tables shared across projects → `core.*` or `audit.*`; project-specific → `pickup.*`, `selector.*`, `signal.*`

### Writing a new migration

```sql
-- migrations/TXXX-brief-name.sql
CREATE TABLE IF NOT EXISTS core.new_table (
    symbol VARCHAR(10) NOT NULL,
    trade_date DATE NOT NULL,
    -- ... columns ...

    source VARCHAR(100),
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    PRIMARY KEY(symbol, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_core_new_table_trade_date ON core.new_table(trade_date);
CREATE INDEX IF NOT EXISTS idx_core_new_table_symbol ON core.new_table(symbol);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_core_new_table_source_role'
    ) THEN
        ALTER TABLE core.new_table
        ADD CONSTRAINT chk_core_new_table_source_role
        CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'));
    END IF;
END $$;
```

## Contributing

1. Modify `core/schema.sql` or `pickup/schema.sql` for schema changes
2. Add a migration file in `migrations/` if the migration is incremental
3. Run `scripts/migrate_to_core.py` to apply
4. Verify with `scripts/backfill_from_mcp.py --dry-run`

---

## Appendix: Related repositories

| Repository | Purpose |
|---|---|
| `tw-quant-pickup` | Pickup pipeline (collectors, API, pipeline_screener) — writes `core.*` as CANONICAL |
| `tw-quant` | Legacy tw-quant pipeline (common/cache.py, factors.py, pipeline_screener.py) |
| `tw-quant-mcp` | MCP fetchers (TWSE/OpenAPI, FinMind) — data source for backfill |
| `tw-quant-signal` | Signal/selector API and frontend |
| `tw-quant-selector` | Portfolio backtesting and selection |
| `tw-quant-daybrain` | Daybrain prediction model |

---
## License

本專案採用 **Apache License 2.0** 授權。

- 完整授權條款見 [`LICENSE`](LICENSE)（專案根目錄）
- Apache-2.0 官方條款：<https://www.apache.org/licenses/LICENSE-2.0>
- 版權與貢獻者資訊以 LICENSE 檔案為準

> 本專案為研究/模擬用途，授權條款不構成任何投資建議或保證；
> 使用/修改/再散佈前請詳閱 LICENSE 全文。

本專案僅供個人量化研究與教育用途。資料來源（FinMind、TWSE、TPEX）之使用請遵守各平台之服務條款。

Proprietary - All rights reserved.
