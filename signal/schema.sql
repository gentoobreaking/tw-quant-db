-- signal/schema.sql
-- PostgreSQL port of tw-quant-signal SQLite schema (db.py: _init_schema)
-- SQLite → PostgreSQL type mappings:
--   TEXT → VARCHAR/TEXT
--   REAL → NUMERIC(20,8)
--   INTEGER → BIGINT (for autoshare) / INTEGER (for data)
--   AUTOINCREMENT → BIGSERIAL
--   datetime('now','localtime') → NOW()

CREATE SCHEMA IF NOT EXISTS signal;

-- ============================================================
-- pipeline_log
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.pipeline_log (
    id          BIGSERIAL PRIMARY KEY,
    run_date    TEXT NOT NULL,
    task        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK(status IN ('ok','fail','skip')),
    message     TEXT,
    created_at  TEXT NOT NULL DEFAULT (NOW()::text)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_run ON signal.pipeline_log(run_date, task);

-- ============================================================
-- daily_prices  (signal uses stock_id, core uses symbol)
-- Per spec §8.1: signal reads core.daily_prices (read-only), no local writes
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.daily_prices (
    stock_id    TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    open        NUMERIC(20,8),
    high        NUMERIC(20,8),
    low         NUMERIC(20,8),
    close       NUMERIC(20,8),
    volume      BIGINT,
    amount      NUMERIC(20,8),
    adj_factor  NUMERIC(20,8) DEFAULT 1.0,
    adj_close   NUMERIC(20,8),
    PRIMARY KEY (stock_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_date_stock ON signal.daily_prices(trade_date, stock_id);

-- ============================================================
-- market_index
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.market_index (
    trade_date  DATE PRIMARY KEY,
    close       NUMERIC(20,8),
    change_pct  NUMERIC(20,8)
);

-- ============================================================
-- institutional_flows
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.institutional_flows (
    stock_id   TEXT NOT NULL,
    trade_date DATE NOT NULL,
    market     TEXT NOT NULL DEFAULT 'TSE',
    foreign_investors_net  BIGINT,
    sity_investors_net     BIGINT,
    dealer_net             BIGINT,
    dealer_proprietary_net BIGINT,
    dealer_hedge_net       BIGINT,
    total_net              BIGINT,
    PRIMARY KEY (stock_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_inst_flows_date_stock ON signal.institutional_flows(trade_date, stock_id);

-- ============================================================
-- signals
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.signals (
    trade_date   DATE NOT NULL,
    stock_id     TEXT NOT NULL,
    d1_score     INTEGER DEFAULT 0,
    d1_signal    TEXT,
    d2_score     INTEGER DEFAULT 0,
    d2_signal    TEXT,
    d3_score     INTEGER DEFAULT 0,
    d3_signal    TEXT,
    d4_score     INTEGER DEFAULT 0,
    d4_signal    TEXT,
    total_score  INTEGER DEFAULT 0,
    signal       TEXT,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- rule_signals
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.rule_signals (
    trade_date      DATE NOT NULL,
    stock_id        TEXT NOT NULL,
    triggered_rules TEXT,
    triggered_count INTEGER DEFAULT 0,
    signal          TEXT,
    total_score     INTEGER DEFAULT 0,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- features
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.features (
    trade_date DATE NOT NULL,
    stock_id   TEXT NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- tech_indicators
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.tech_indicators (
    stock_id       TEXT NOT NULL,
    trade_date     DATE NOT NULL,
    ma5            NUMERIC(20,8),
    ma20           NUMERIC(20,8),
    ma60           NUMERIC(20,8),
    bb_upper       NUMERIC(20,8),
    bb_middle      NUMERIC(20,8),
    bb_lower       NUMERIC(20,8),
    rsi14          NUMERIC(20,8),
    volume_ma5     NUMERIC(20,8),
    volume_ma20    NUMERIC(20,8),
    PRIMARY KEY (stock_id, trade_date)
);

-- ============================================================
-- financial_data
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.financial_data (
    stock_id        TEXT NOT NULL,
    fiscal_quarter  TEXT NOT NULL,
    eps             NUMERIC(20,8),
    revenue         NUMERIC(20,8),
    gross_margin    NUMERIC(20,8),
    updated_at      TEXT NOT NULL DEFAULT (NOW()::text),
    PRIMARY KEY (stock_id, fiscal_quarter)
);

-- ============================================================
-- margin_data
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.margin_data (
    stock_id        TEXT NOT NULL,
    trade_date      DATE NOT NULL,
    margin_balance  BIGINT,
    short_balance   BIGINT,
    margin_ratio    NUMERIC(20,8),
    PRIMARY KEY (stock_id, trade_date)
);

-- ============================================================
-- quarterly_financials
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.quarterly_financials (
    stock_id        TEXT NOT NULL,
    fiscal_quarter  TEXT NOT NULL,
    eps             NUMERIC(20,8),
    revenue         NUMERIC(20,8),
    gross_margin    NUMERIC(20,8),
    roe             NUMERIC(20,8),
    roa             NUMERIC(20,8),
    updated_at      TEXT NOT NULL DEFAULT (NOW()::text),
    PRIMARY KEY (stock_id, fiscal_quarter)
);

-- ============================================================
-- dividends
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.dividends (
    stock_id        TEXT NOT NULL,
    year            INTEGER NOT NULL,
    ex_date         TEXT,
    close_before_ex  NUMERIC(20,8),
    cash_dividend   NUMERIC(20,8),
    cash_pay_date   TEXT,
    cash_yield      NUMERIC(20,8),
    stock_dividend  NUMERIC(20,8),
    PRIMARY KEY (stock_id, year)
);

-- ============================================================
-- margin_trading
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.margin_trading (
    stock_id    TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    margin_buy      BIGINT,
    margin_sell     BIGINT,
    margin_balance  BIGINT,
    short_sell      BIGINT,
    short_buy       BIGINT,
    short_balance   BIGINT,
    PRIMARY KEY (stock_id, trade_date)
);

-- ============================================================
-- risk_metrics
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.risk_metrics (
    trade_date      DATE NOT NULL,
    stock_id        TEXT NOT NULL,
    volatility_20d  NUMERIC(20,8),
    volatility_avg  NUMERIC(20,8),
    vol_ratio       NUMERIC(20,8),
    atr_14d         NUMERIC(20,8),
    atr_pct         NUMERIC(20,8),
    max_drawdown    NUMERIC(20,8),
    signal_conflict INTEGER DEFAULT 0,
    stop_loss_atr   NUMERIC(20,8),
    stop_loss_ma    NUMERIC(20,8),
    risk_level      TEXT,
    risk_score      INTEGER DEFAULT 0,
    details         TEXT,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- health_scores
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.health_scores (
    trade_date          DATE NOT NULL,
    stock_id            TEXT NOT NULL,
    fundamental_score   NUMERIC(20,8),
    fundamental_light   TEXT,
    institutional_score NUMERIC(20,8),
    institutional_light TEXT,
    technical_score     NUMERIC(20,8),
    technical_light     TEXT,
    valuation_score     NUMERIC(20,8),
    valuation_light     TEXT,
    total_score         NUMERIC(20,8),
    total_light         TEXT,
    details             TEXT,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- weekly_indicators
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.weekly_indicators (
    stock_id       TEXT NOT NULL,
    trade_date     DATE NOT NULL,
    close          NUMERIC(20,8),
    ma5            NUMERIC(20,8),
    ma20           NUMERIC(20,8),
    ma60           NUMERIC(20,8),
    bb_upper       NUMERIC(20,8),
    bb_middle      NUMERIC(20,8),
    bb_lower       NUMERIC(20,8),
    rsi14          NUMERIC(20,8),
    volume_ma5     NUMERIC(20,8),
    volume_ma20    NUMERIC(20,8),
    PRIMARY KEY (stock_id, trade_date)
);

-- ============================================================
-- monthly_health_scores
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.monthly_health_scores (
    trade_date          DATE NOT NULL,
    stock_id            TEXT NOT NULL,
    fundamental_score   NUMERIC(20,8),
    fundamental_light   TEXT,
    institutional_score NUMERIC(20,8),
    institutional_light TEXT,
    technical_score     NUMERIC(20,8),
    technical_light     TEXT,
    valuation_score     NUMERIC(20,8),
    valuation_light     TEXT,
    total_score         NUMERIC(20,8),
    total_light         TEXT,
    details             TEXT,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- weekly_health_scores
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.weekly_health_scores (
    trade_date          DATE NOT NULL,
    stock_id            TEXT NOT NULL,
    fundamental_score   NUMERIC(20,8),
    fundamental_light   TEXT,
    institutional_score NUMERIC(20,8),
    institutional_light TEXT,
    technical_score     NUMERIC(20,8),
    technical_light     TEXT,
    valuation_score     NUMERIC(20,8),
    valuation_light     TEXT,
    total_score         NUMERIC(20,8),
    total_light         TEXT,
    details             TEXT,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- multi_timeframe_consensus
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.multi_timeframe_consensus (
    trade_date       DATE NOT NULL,
    stock_id         TEXT NOT NULL,
    daily_light      TEXT,
    weekly_light     TEXT,
    consensus        TEXT NOT NULL,
    consensus_label  TEXT NOT NULL,
    signal_type      TEXT NOT NULL,
    details          TEXT,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- monthly_indicators
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.monthly_indicators (
    stock_id       TEXT NOT NULL,
    trade_date     DATE NOT NULL,
    close          NUMERIC(20,8),
    ma3            NUMERIC(20,8),
    ma6            NUMERIC(20,8),
    ma12           NUMERIC(20,8),
    bb_upper       NUMERIC(20,8),
    bb_middle      NUMERIC(20,8),
    bb_lower       NUMERIC(20,8),
    rsi9           NUMERIC(20,8),
    volume_ma3     NUMERIC(20,8),
    volume_ma6     NUMERIC(20,8),
    PRIMARY KEY (stock_id, trade_date)
);

-- ============================================================
-- structural_drift
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.structural_drift (
    id              BIGSERIAL PRIMARY KEY,
    trade_date      DATE NOT NULL,
    drift_type      TEXT NOT NULL,
    rule_id         TEXT,
    feature_name    TEXT,
    reference_value NUMERIC(20,8),
    recent_value    NUMERIC(20,8),
    drift_score     NUMERIC(20,8),
    drift_status    TEXT,
    direction       TEXT,
    details         TEXT
);

CREATE INDEX IF NOT EXISTS idx_structural_drift ON signal.structural_drift(trade_date, drift_type);

-- ============================================================
-- operation_log
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.operation_log (
    id              BIGSERIAL PRIMARY KEY,
    log_date        TEXT NOT NULL,
    stock_id        TEXT,
    action          TEXT NOT NULL,
    signal          TEXT,
    score           INTEGER,
    mode            TEXT,
    rule_version_hash TEXT,
    details         TEXT,
    created_at      TEXT NOT NULL DEFAULT (NOW()::text)
);

-- ============================================================
-- monthly_revenue
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.monthly_revenue (
    stock_id    TEXT NOT NULL,
    year_month  TEXT NOT NULL,
    revenue     NUMERIC(20,8),
    mom_change  NUMERIC(20,8),
    yoy_change  NUMERIC(20,8),
    PRIMARY KEY (stock_id, year_month)
);

-- ============================================================
-- scorecard
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.scorecard (
    trade_date     DATE NOT NULL,
    stock_id       TEXT NOT NULL,
    bullish_score  INTEGER NOT NULL,
    bearish_score  INTEGER NOT NULL,
    bullish_detail TEXT NOT NULL,
    bearish_detail TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_id)
);

-- ============================================================
-- performance_log (T019)
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.performance_log (
    id                BIGSERIAL PRIMARY KEY,
    stock_id          TEXT NOT NULL,
    rule_id           TEXT NOT NULL,
    trigger_date      TEXT NOT NULL,
    market_state      TEXT,
    close_at_trigger  NUMERIC(20,8),
    after_1d_return   NUMERIC(20,8),
    after_3d_return   NUMERIC(20,8),
    after_5d_return   NUMERIC(20,8),
    after_10d_return  NUMERIC(20,8),
    inspection_date   TEXT,
    UNIQUE(stock_id, rule_id, trigger_date)
);

CREATE INDEX IF NOT EXISTS idx_perf_log_trigger ON signal.performance_log(trigger_date);
CREATE INDEX IF NOT EXISTS idx_perf_log_rule ON signal.performance_log(rule_id);
CREATE INDEX IF NOT EXISTS idx_perf_log_stock ON signal.performance_log(stock_id);

-- ============================================================
-- watchlist_history (T010)
-- ============================================================
CREATE TABLE IF NOT EXISTS signal.watchlist_history (
    stock_id       TEXT NOT NULL,
    since_date     TEXT NOT NULL,
    removed_date   TEXT,
    UNIQUE(stock_id, since_date)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_history_status ON signal.watchlist_history(stock_id, removed_date);

-- ============================================================
-- Indexes summary
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_operation_log ON signal.operation_log(log_date, action);
