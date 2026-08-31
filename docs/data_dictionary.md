# tw-quant-db Data Dictionary

## core Schema

### core.stocks

Stock master data. Updated by tw-quant-pickup from TWSE/TWSE listings.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `symbol` | VARCHAR(10) | NO | — | Stock symbol (e.g., `2330`, `GOLD`) |
| `name` | VARCHAR(100) | NO | — | Stock name in Chinese |
| `market` | VARCHAR(20) | NO | — | Market: `TWSE`, `OTC`, `OtcPink`, `GLOBAL` |
| `sector` | VARCHAR(100) | YES | — | Sector classification (e.g., `半導體業`) |
| `industry` | VARCHAR(100) | YES | — | Industry classification |
| `security_type` | VARCHAR(20) | NO | — | `STOCK`, `ETF`, `WARRANT`, `REIT` |
| `listed_date` | DATE | YES | — | Date first listed |
| `active` | BOOLEAN | YES | `true` | Whether the stock is currently active |
| `needs_manual_review` | BOOLEAN | YES | `false` | 回補失敗標記（backfill 斷點續跑用） |
| `created_at` | TIMESTAMP | NO | — | Record creation time |
| `updated_at` | TIMESTAMP | NO | — | Last update time |
**Indexes**: `stocks_pkey` (PK: symbol)

---

### core.trading_calendar

回補缺口偵測用交易日曆（`backfill` 5 天批次、`getMissingDates` 依此判斷交易日，週末/假日跳過）。

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `trade_date` | DATE | NO | — | 曆日 |
| `is_trading` | BOOLEAN | NO | `true` | 是否為交易日 |
| `day_of_week` | INTEGER | YES | — | 0=週日, 6=週六 |

**Indexes**: PK(trade_date), `idx_core_trading_calendar_is_trading`
**關聯**: `TRADING_CALENDAR ||--o{ DAILY_PRICES`（backfill 缺口 → daily_prices）

---

### core.daily_prices

Daily price data. Updated by tw-quant-pickup from yfinance/MCP feeds.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `symbol` | VARCHAR(10) | NO | — | Stock/symbol identifier (e.g., `2330`, `GOLD`) |
| `trade_date` | DATE | NO | — | Trading date |
| `open` | NUMERIC(14,4) | YES | — | Opening price |
| `high` | NUMERIC(14,4) | YES | — | Highest price |
| `low` | NUMERIC(14,4) | YES | — | Lowest price |
| `close` | NUMERIC(14,4) | YES | — | Closing price |
| `adjusted_close` | NUMERIC(14,4) | YES | — | Adjusted close (for dividends/splits) |
| `volume` | BIGINT | YES | — | Trading volume |
| `turnover` | NUMERIC(20,2) | YES | — | Turnover (price × volume) |
| `source` | VARCHAR(100) | YES | — | Source identifier (e.g., `yfinance`, `mcp`) |
| `data_date` | DATE | YES | — | Date source data was received |
| `freshness` | VARCHAR(30) | YES | — | Freshness indicator (e.g., `REALTIME`, `END_OF_DAY`) |
| `source_role` | VARCHAR(30) | NO | `'CANONICAL'` | Source trust level: `CANONICAL`, `SEMI_OFFICIAL_REALTIME`, `FALLBACK` |

**Constraints**: PK(symbol, trade_date); CHECK source_role IN (...)

**Indexes**: `daily_prices_pkey`, `idx_core_daily_prices_symbol`, `idx_core_daily_prices_trade_date`
**Planned (Phase 3)**: Monthly RANGE partitioning by trade_date; BRIN index on trade_date

---

### core.financials

Quarterly financial statements. Updated by tw-quant-pickup.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `symbol` | VARCHAR(10) | NO | — | Stock symbol |
| `fiscal_year` | INTEGER | NO | — | Fiscal year |
| `fiscal_quarter` | INTEGER | NO | — | Fiscal quarter (1-4) |
| `revision` | INTEGER | NO | `1` | Revision number (for corrections) |
| `revenue` | NUMERIC(20,4) | YES | — | Total revenue |
| `gross_profit` | NUMERIC(20,4) | YES | — | Gross profit |
| `operating_income` | NUMERIC(20,4) | YES | — | Operating income |
| `net_income` | NUMERIC(20,4) | YES | — | Net income |
| `eps` | NUMERIC(14,4) | YES | — | Earnings per share |
| `book_value_per_share` | NUMERIC(14,4) | YES | — | Book value per share |
| `total_assets` | NUMERIC(20,4) | YES | — | Total assets |
| `total_liabilities` | NUMERIC(20,4) | YES | — | Total liabilities |
| `equity` | NUMERIC(20,4) | YES | — | Shareholders' equity |
| `roe` | NUMERIC(10,4) | YES | — | Return on equity |
| `roa` | NUMERIC(10,4) | YES | — | Return on assets |
| `operating_cash_flow` | NUMERIC(20,4) | YES | — | Operating cash flow |
| `investing_cash_flow` | NUMERIC(20,4) | YES | — | Investing cash flow |
| `capex` | NUMERIC(20,4) | YES | — | Capital expenditures |
| `free_cash_flow` | NUMERIC(20,4) | YES | — | Free cash flow |
| `reported_at` | DATE | NO | — | Report publication date |
| `observed_at` | TIMESTAMP | NO | — | When data was observed/ingested |
| `source` | VARCHAR(100) | YES | — | Source identifier |
| `source_timestamp` | TIMESTAMP | YES | — | Timestamp from source |
| `data_date` | DATE | YES | — | Date source data was received |
| `freshness` | VARCHAR(30) | YES | — | Freshness indicator |
| `source_role` | VARCHAR(30) | NO | `'CANONICAL'` | Source trust level |

**Constraints**: PK(symbol, fiscal_year, fiscal_quarter, revision); CHECK source_role IN (...)

**Indexes**: `financials_pkey`, `idx_core_financials_symbol`

---

### core.monthly_revenues

Monthly revenue data. Updated by tw-quant-pickup from TWSE.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `symbol` | VARCHAR(10) | NO | — | Stock symbol |
| `year_month` | DATE | NO | — | Year-month of revenue |
| `revenue` | NUMERIC(20,4) | YES | — | Monthly revenue |
| `yoy_growth` | NUMERIC(10,4) | YES | — | Year-over-year growth (%) |
| `mom_growth` | NUMERIC(10,4) | YES | — | Month-over-month growth (%) |
| `cumulative_revenue` | NUMERIC(20,4) | YES | — | Cumulative revenue for YTD |
| `reported_at` | DATE | YES | — | Report date |
| `observed_at` | TIMESTAMP | NO | — | Ingest timestamp |
| `source` | VARCHAR(100) | YES | — | Source identifier |
| `data_date` | DATE | YES | — | Source data date |
| `freshness` | VARCHAR(30) | YES | — | Freshness indicator |
| `source_role` | VARCHAR(30) | NO | `'CANONICAL'` | Source trust level |

**Constraints**: PK(symbol, year_month); CHECK source_role IN (...)

**Indexes**: `monthly_revenues_pkey`, `idx_core_monthly_revenues_symbol`

---

### core.dividends

Dividend data. Updated by tw-quant-pickup.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `symbol` | VARCHAR(10) | NO | — | Stock symbol |
| `fiscal_year` | INTEGER | NO | — | Fiscal year |
| `cash_dividend` | NUMERIC(14,4) | YES | — | Cash dividend per share |
| `stock_dividend` | NUMERIC(14,4) | YES | — | Stock dividend ratio |
| `payout_ratio` | NUMERIC(10,4) | YES | — | Payout ratio (dividend/EPS) |
| `ex_date` | DATE | YES | — | Ex-dividend date |
| `payment_date` | DATE | YES | — | Payment date |
| `source` | VARCHAR(100) | YES | — | Source identifier |
| `data_date` | DATE | YES | — | Source data date |
| `freshness` | VARCHAR(30) | YES | — | Freshness indicator |
| `source_role` | VARCHAR(30) | NO | `'CANONICAL'` | Source trust level |

**Constraints**: PK(symbol, fiscal_year); CHECK source_role IN (...)

**Indexes**: `dividends_pkey`

---

### core.institutional_flow

Institutional trading flow (foreign, investment trust, dealer nets).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `symbol` | VARCHAR(10) | NO | — | Stock symbol |
| `trade_date` | DATE | NO | — | Trading date |
| `foreign_net` | BIGINT | YES | — | Foreign investor net (shares) |
| `investment_trust_net` | BIGINT | YES | — | Investment trust net (shares) |
| `dealer_net` | BIGINT | YES | — | Dealer ( Proprietary Trading ) net (shares) |
| `total_net` | BIGINT | YES | — | Total institutional net |
| `availability_date` | DATE | NO | — | Date the data becomes available |
| `source` | VARCHAR(100) | YES | — | Source identifier |
| `data_date` | DATE | YES | — | Source data date |
| `freshness` | VARCHAR(30) | YES | — | Freshness indicator |
| `source_role` | VARCHAR(30) | NO | `'CANONICAL'` | Source trust level |

**Constraints**: PK(symbol, trade_date); CHECK source_role IN (...)

**Indexes**: `institutional_flow_pkey`, `idx_core_institutional_flow_trade_date`

---

### core.market_context

Options and futures market context data.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `context_type` | VARCHAR(30) | NO | — | Context type: `FUTURE`, `OPTION`, `INDEX` |
| `symbol` | VARCHAR(30) | NO | — | Symbol (e.g., `TX`, `TXO`) |
| `trade_date` | DATE | NO | — | Trading date |
| `close` | NUMERIC(20,4) | YES | — | Closing price/index |
| `change` | NUMERIC(20,4) | YES | — | Price change |
| `change_percent` | NUMERIC(10,4) | YES | — | Change percentage |
| `call_volume` | BIGINT | YES | — | Call option volume |
| `put_volume` | BIGINT | YES | — | Put option volume |
| `call_oi` | BIGINT | YES | — | Call open interest |
| `put_oi` | BIGINT | YES | — | Put open interest |
| `volume_ratio` | NUMERIC(10,4) | YES | — | Put/call volume ratio |
| `oi_ratio` | NUMERIC(10,4) | YES | — | Put/call OI ratio |
| `contract` | VARCHAR(20) | YES | — | Contract name |
| `contract_month` | VARCHAR(10) | YES | — | Contract month |
| `session` | VARCHAR(10) | YES | — | Trading session |
| `open` | NUMERIC(20,4) | YES | — | Opening price |
| `high` | NUMERIC(20,4) | YES | — | Highest price |
| `low` | NUMERIC(20,4) | YES | — | Lowest price |
| `volume` | BIGINT | YES | — | Trading volume |
| `settlement` | NUMERIC(20,4) | YES | — | Settlement price |
| `open_interest` | BIGINT | YES | — | Open interest |
| `unit` | VARCHAR(10) | YES | — | Price unit (e.g., `TWD`) |
| `payload` | JSONB | YES | — | Additional raw data |
| `source` | VARCHAR(100) | YES | — | Source identifier |
| `data_date` | DATE | YES | — | Source data date |
| `freshness` | VARCHAR(30) | YES | — | Freshness indicator |
| `source_role` | VARCHAR(30) | NO | `'CANONICAL'` | Source trust level |
| `observed_at` | TIMESTAMP | NO | `NOW()` | Ingest timestamp |

**Constraints**: PK(context_type, symbol, trade_date); CHECK source_role IN (...)

**Indexes**: `market_context_pkey`, `idx_core_market_context_trade_date`, `idx_core_market_context_type_symbol`

---

### core.universe_flags

Watchlist/universe flags for stocks.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `symbol` | VARCHAR(10) | NO | — | Stock symbol |
| `flag_date` | DATE | NO | — | Flag date |
| `attention` | BOOLEAN | YES | `false` | Needs attention |
| `disposition` | BOOLEAN | YES | `false` | Disposition flagged |
| `disposition_reason` | TEXT | YES | — | Reason for disposition flag |
| `suspended` | BOOLEAN | YES | `false` | Trading suspended |

**Constraints**: PK(symbol, flag_date)

**Indexes**: `universe_flags_pkey`, `idx_core_universe_flags_flag_date`

---

### core.margin_trading

Margin trading and short selling data.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `symbol` | VARCHAR(10) | NO | — | Stock symbol |
| `trade_date` | DATE | NO | — | Trading date |
| `margin_buy` | BIGINT | YES | — | Marginbuy (螌金買入) |
| `margin_sell` | BIGINT | YES | — | Margin sell (螌金賣出) |
| `margin_balance` | BIGINT | YES | — | Margin balance (螌金餘額) |
| `margin_limit` | BIGINT | YES | — | Margin credit limit (螌金額度) |
| `short_buy` | BIGINT | YES | — | Short covering (借券购回) |
| `short_sell` | BIGINT | YES | — | Short selling (借券賣出) |
| `short_balance` | BIGINT | YES | — | Short balance (借券餘額) |
| `short_limit` | BIGINT | YES | — | Short credit limit (借券額度) |
| `offset` | BIGINT | YES | — | Net offset (螌金淨餘減借券淨餘) |
| `source` | VARCHAR(100) | YES | — | Source identifier |
| `data_date` | DATE | YES | — | Source data date |
| `freshness` | VARCHAR(30) | YES | — | Freshness indicator |
| `source_role` | VARCHAR(30) | NO | `'CANONICAL'` | Source trust level |

**Constraints**: PK(symbol, trade_date); CHECK source_role IN (...)

**Indexes**: `margin_trading_pkey`, `idx_core_margin_trading_symbol`, `idx_core_margin_trading_trade_date`

---

### core.alerts

User-defined price alerts. Created by gold-analysis backend (T019).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | BIGSERIAL | NO | — | Auto-increment primary key |
| `user_id` | INTEGER | NO | — | References `public.users.id` |
| `alert_type` | VARCHAR(20) | NO | — | Alert type (e.g., `PRICE_ABOVE`, `PRICE_BELOW`) |
| `asset` | VARCHAR(20) | NO | — | Asset symbol (e.g., `GOLD`) |
| `target_price` | NUMERIC(14,4) | NO | — | Target trigger price |
| `is_active` | BOOLEAN | YES | `true` | Whether alert is active |
| `created_at` | TIMESTAMP | NO | `NOW()` | Alert creation time |
| `triggered_at` | TIMESTAMP | YES | — | When alert was triggered |
| `extra_data` | VARCHAR(500) | YES | — | Additional JSON metadata |

**Constraints**: PK(id); FK(user_id → public.users.id)

**Indexes**: `alerts_pkey`, `idx_core_alerts_user_id`, `idx_core_alerts_asset`, `idx_core_alerts_created_at`

---

### core.decisions

Trading decisions made by the gold-analysis system. Created by gold-analysis backend (T019).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | BIGSERIAL | NO | — | Auto-increment primary key |
| `user_id` | INTEGER | YES | — | References `public.users.id` |
| `portfolio_id` | INTEGER | YES | — | References `public.portfolios.id` |
| `decision_type` | VARCHAR(20) | NO | — | `BUY`, `SELL`, `HOLD` |
| `source` | VARCHAR(50) | NO | — | Decision source (e.g., `LLM`, `RULE`) |
| `asset` | VARCHAR(20) | YES | `'GOLD'` | Asset symbol |
| `signal_strength` | REAL | NO | — | Signal strength score (0-1) |
| `confidence` | REAL | NO | — | Confidence score (0-1) |
| `price_target` | NUMERIC(14,4) | YES | — | Target price |
| `stop_loss` | NUMERIC(14,4) | YES | — | Stop loss price |
| `reason_zh` | TEXT | YES | — | Reason (Chinese) |
| `reason_en` | TEXT | YES | — | Reason (English) |
| `indicators_snapshot` | TEXT | YES | — | JSON snapshot of indicators |
| `analysis_scores` | TEXT | YES | — | JSON analysis scores |
| `is_executed` | BOOLEAN | YES | `false` | Whether decision was executed |
| `executed_at` | TIMESTAMP | YES | — | Execution timestamp |
| `execution_price` | NUMERIC(14,4) | YES | — | Execution price |
| `model_version` | VARCHAR(50) | YES | `'v1'` | Model version |
| `extra_data` | TEXT | YES | — | Additional JSON metadata |
| `created_at` | TIMESTAMP | NO | `NOW()` | Creation timestamp |
| `updated_at` | TIMESTAMP | NO | `NOW()` | Last update timestamp |

**Constraints**: PK(id); FK(user_id → public.users.id); FK(portfolio_id → public.portfolios.id)

**Indexes**: `decisions_pkey`, `idx_core_decisions_user_id`, `idx_core_decisions_asset`, `idx_core_decisions_created_at`

---

## public Schema (gold-analysis)

### public.users

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | INTEGER | NO | Primary key |
| `username` | VARCHAR | NO | Username |
| `email` | VARCHAR | NO | Email |
| `hashed_password` | VARCHAR | NO | Hashed password |
| `display_name` | VARCHAR | YES | Display name |
| `avatar_url` | VARCHAR | YES | Avatar URL |
| `bio` | TEXT | YES | User bio |
| `timezone` | VARCHAR | YES | Timezone |
| `language` | VARCHAR | YES | Language |
| `is_active` | BOOLEAN | YES | Active flag |
| `is_verified` | BOOLEAN | YES | Email verified |
| `is_premium` | BOOLEAN | YES | Premium flag |
| `created_at` | TIMESTAMP | YES | Creation time |
| `updated_at` | TIMESTAMP | YES | Last update |
| `last_login` | TIMESTAMP | YES | Last login |

### public.portfolios

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | INTEGER | NO | Primary key |
| `user_id` | INTEGER | NO | FK → users.id |
| `name` | VARCHAR | NO | Portfolio name |
| `description` | VARCHAR | YES | Description |
| `initial_capital` | DOUBLE | NO | Initial capital |
| `current_value` | DOUBLE | YES | Current value |
| `created_at` | TIMESTAMP | YES | Creation time |
| `updated_at` | TIMESTAMP | YES | Last update |

### public.portfolio_holdings

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `id` | INTEGER | NO | Primary key |
| `portfolio_id` | INTEGER | NO | FK → portfolios.id |
| `asset_type` | VARCHAR | NO | Asset type |
| `quantity` | DOUBLE | NO | Quantity held |
| `avg_cost` | DOUBLE | NO | Average cost |
| `current_price` | DOUBLE | YES | Current price |
| `market_value` | DOUBLE | YES | Market value |
| `created_at` | TIMESTAMP | YES | Creation time |
| `updated_at` | TIMESTAMP | YES | Last update |

---

## Lineage Columns Convention

Every `core` fact table (daily_prices, financials, monthly_revenues, dividends, institutional_flow, market_context, margin_trading) includes four lineage columns:

| Column | Type | Description |
|--------|------|-------------|
| `source` | VARCHAR(100) | Identifier of the data source (e.g., `yfinance`, `mcp`, `twse`) |
| `data_date` | DATE | Date the source data was generated/observed |
| `freshness` | VARCHAR(30) | How fresh the data is (e.g., `REALTIME`, `END_OF_DAY`, `CORRECTED`) |
| `source_role` | VARCHAR(30) | Trust level: `CANONICAL` (authoritative), `SEMI_OFFICIAL_REALTIME` (real-time, verify), `FALLBACK` (fallback data) |

Tables without lineage columns: `stocks`, `universe_flags`, `alerts`, `decisions`.
