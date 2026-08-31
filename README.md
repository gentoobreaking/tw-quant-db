# tw-quant-db

Shared PostgreSQL schema definition and migration/backfill scripts for the tw-quant ecosystem. This repository owns the **data layer** — the `core`, `pickup`, `selector`, `signal`, and `audit` schemas — plus scripts that migrate data into `core` and verify integrity. It does **not** implement data collection pipelines; those live in [`tw-quant-pickup`](https://github.com/gentoobreaking/tw-quant) (the pickup pipeline) and [`tw-quant-mcp`](https://github.com/gentoobreaking/tw-quant) (TwseOpenAPI / FinMind fetchers).

## Overview

Tw-quant is a Taiwanese stock market analysis system. Multiple projects (pickup, selector, signal, daybrain, mcp) previously each maintained their own SQLite databases with overlapping but incompatible schemas. This repository centralizes the **shared PostgreSQL data layer**:

- **core schema**: raw/fact tables — single source of truth, written only by the pickup pipeline
- **pickup schema**: tw-quant-pickup business logic tables (factor_scores, valuations, rankings, audit logs)
- **selector schema**: tw-quant-selector tables (portfolio, backtest, alerts)
- **signal schema**: tw-quant-signal technical indicator tables
- **audit schema**: shared audit tables (operation_logs)

## Features

- PostgreSQL schema definitions for 5 schemas (`core/`, `pickup/`, `signal/`, `init-scripts/`)
- **Go backfill service** (`backfill/`): MCP 多源 fallback（local-mcp → twse-mcp → finmind-mcp → yfinance-mcp）自動補齊 `core.daily_prices` 缺口，支援 7d/1mo/5Y、斷點續跑、交易日曆判斷（詳見 [docs/backfill.md](docs/backfill.md)）
- Migration script (`scripts/migrate_to_core.py`): copies existing pickup data → core schema
- MCP cache backfill (`scripts/backfill_from_mcp.py`): bulk-imports 4,818 cache entries from tw-quant-mcp's `cache.db` into `core.*` with `source_role='FALLBACK'`
- Signal backfill (`scripts/backfill_from_signal.py`): imports data from tw-quant-signal SQLite → core
- Signal-to-PostgreSQL migration (`scripts/migrate_signal_to_pg.py`)
- Compatibility views for projects with different column naming conventions (`symbol` vs `stock_id`)
- Idempotent schema creation with `CREATE TABLE IF NOT EXISTS` and `DO $$ ... END $$` constraint guards
- Shared `tw-quant-network`（`external: true`）與 `tw-quant-mcp` 共用，`docker compose up -d` 自動 7 天回補

## Architecture

```
tw-quant-db/                    ← this repo (schema + scripts)
├── core/schema.sql             ← core.* fact tables + indexes + constraints
├── pickup/schema.sql           ← pickup.* business tables
├── backfill/                   ← Go 回補服務（MCP fallback chain）
├── migrations/                 ← incremental SQL migrations
├── init-scripts/               ← Docker entrypoint init (schema creation)
├── scripts/                    ← Python migration & backfill scripts
└── docker-compose.yml          ← shared PostgreSQL + pgAdmin + backfill(7d)

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

### Data Lineage Model

Every row in `core.*` tables includes three lineage columns:
- `source`: which system wrote the row (e.g., `"tw-quant-mcp"`, `"tw-quant-pickup"`)
- `data_date`: when the underlying data was valid
- `freshness`: `"raw"` (from source) or `"processed"` (computed)
- `source_role`: `"CANONICAL"` (pickup pipeline, primary), `"SEMI_OFFICIAL_REALTIME"` (real-time), or `"FALLBACK"` (backfilled from cache)

All `core.*` tables have a `CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'))` constraint.

## Project Structure

```
tw-quant-db/
├── core/
│   └── schema.sql          # 8 core fact tables + indexes + constraints
├── backfill/               # Go 回補服務（MCP fallback chain, docs/backfill.md）
│   ├── backfill.go         # 缺口偵測、批次、fallback、checkpoint
│   ├── sources.go          # local/twse/finmind/yfinance MCP clients
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
│   ├── migrate_to_core.py        # pickup → core data migration
│   ├── backfill_from_mcp.py      # MCP cache.db → core (FALLBACK)
│   ├── backfill_from_signal.py   # signal SQLite → core
│   └── migrate_signal_to_pg.py   # signal SQLite → PostgreSQL
├── docker-compose.yml       # shared PostgreSQL + pgAdmin + backfill(7d, external tw-quant-network)
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
docker network create tw-quant-network  # 與 tw-quant-mcp 共用（external: true）
# .env 已含 POSTGRES_PASSWORD / FINMIND_TOKEN（gitignored），若更新：
echo "POSTGRES_PASSWORD=$(cat secrets/postgres_password.txt)" > .env
echo "FINMIND_TOKEN=$(cat ~/.finmind_token)" >> .env
```

### 2. 啟動共享 PostgreSQL（自動 7 天回補）

```bash
cd tw-quant-db
docker compose up -d
# 啟動：postgres:5432 + pgadmin:5050 + backfill(7d 全市場實寫，local-mcp 優先)
# 需 tw-quant-mcp 已在別專案 `docker compose up -d`（同 tw-quant-network）
```

This starts:
- PostgreSQL on port 5432 (user: `twquant`, database: `twquant_shared`)
- pgAdmin on port 5450 (email: `admin@twquant.local`)
- backfill: 近 7 天全市場 `core.daily_prices` 回補（`BACKFILL_ALL_LISTED=true`）

Secrets are mounted from `./secrets/` (gitignored). See `secrets/` for required files.

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
| `DATABASE_URL` | `postgresql://localhost:5432/twquant_shared` | PostgreSQL connection string |
| `MCP_CACHE_DB` | `~/Projects/tw-quant-mcp/data/cache.db` | Path to tw-quant-mcp cache.db |
| `MCP_HOST` | `http://tw-quant-mcp:8000` | tw-quant-mcp streamable-http |
| `FINMIND_TOKEN` | - | FinMind API token (fallback 2) |
| `BACKFILL_ALL_LISTED` | `true` (compose 預設) | 全市場回補 |
| `STOCK_IDS` | - | 指定 `2330,0050` |
| `STOCKS_FILE` | - | 外部清單每行一檔 |

### Database Connection

```
postgresql://twquant:<password>@localhost:5432/twquant_shared
```

Password is in `secrets/postgres_password.txt` (gitignored).

## Quick Start

```bash
# 1. 啟動共享網路與 tw-quant-mcp（別專案）
docker network create tw-quant-network
cd ~/Projects/tw-quant-mcp && docker compose up -d

# 2. 啟動 tw-quant-db（自動 7 天全市場回補）
cd ~/Projects/tw-quant-db && docker compose up -d
# → postgres:5432 + pgadmin:5050 + backfill(7d, local-mcp 優先)

# 3. 手動回補（覆蓋參數）
docker compose run --rm backfill --stock 2330 --dry-run --range 1mo
docker compose run --rm backfill --range 1Y --sources mcp
# 詳見 docs/backfill.md
```

## Usage

### Go 回補（MCP fallback chain → core.daily_prices）

`backfill/` 為 Go 二進位，透過 MCP 多源自動補齊缺口，`coverage ≥0.7` 才寫入，`trading_calendar` 判定交易日，`ON CONFLICT DO UPDATE` 保 idempotent。

```bash
# 全市場近 7 天（compose 預設，up 即自動跑）
docker compose run --rm backfill
# 單檔試跑 / 指定區間 / 斷點續跑
docker compose run --rm backfill --stock 2330 --dry-run --range 1mo
docker compose run --rm backfill --stock-ids "2330,0050" --start 2025-08-25 --end 2026-08-28 --sources mcp
docker compose run --rm backfill --range 1Y --sources mcp --resume
```
詳見 [docs/backfill.md](docs/backfill.md)。

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

## Error Handling

- **Schema creation**: All `CREATE TABLE` statements use `IF NOT EXISTS`; constraint additions use `DO $$ BEGIN ... IF NOT EXISTS ... END $$` guards for idempotency.
- **Duplicate data**: Backfill scripts use `INSERT ... ON CONFLICT DO NOTHING` to avoid overwriting existing rows (preserves CANONICAL data from the pickup pipeline).
- **Connection errors**: `backfill_from_mcp.py` catches `asyncpg` connection errors and exits with code 1.

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

No build step — SQL schema files and Python scripts are used directly.

## Deployment

```
tw-quant-db/
├── core/                    # core.* schema
├── init-scripts/            # Docker entrypoint: creates all 5 schemas
├── migrations/              # Incremental migrations (T011, T017, T018)
├── scripts/                 # Python migration/backfill scripts
├── docker-compose.yml       # PostgreSQL 16 + pgAdmin
└── secrets/                 # gitignored password files
```

Deploy steps:
1. Clone this repo
2. Copy password files to `secrets/` (or mount Docker secrets)
3. `docker compose up -d`
4. Run migration/backfill scripts as needed

## Limitations

- No test suite in this repository (tests live in `tw-quant-pickup`)
- `backfill_from_mcp.py` margin backfill: 2 of 3 cache entries decode to stock-level data (1,295 rows); 1 entry uses a different encoding format and is skipped
- `backfill_from_mcp.py` daily_kline: 62 of 77 cache entries are candle data (timestamp/open/high/low/close/volume) that require key reversal; some keys cannot be reversed due to missing stock code, resulting in 65 rows inserted (some candle entries produce multiple daily price records)
- `backfill_from_mcp.py` financials: 4,215 cache entries produce 32,036 records, but after `ON CONFLICT DO NOTHING` (deduplication by primary key), only 3,462 unique rows remain. The task acceptance criterion of "≥4,215 rows" was based on cache entry count, not unique DB rows
- tw-quant-signal's `common/cache.py` DiskCache PostgreSQL backend is in the `tw-quant` repo (separate project)

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

## License

[License: MIT](LICENSE)

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
