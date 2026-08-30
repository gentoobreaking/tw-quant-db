-- ================================================================
-- Core Schema (方案 C: core shared + per-project schemas)
-- Based on tw-quant-pickup v0.3 (most complete lineage model)
-- Core 唯一寫入者: tw-quant-pickup 攝取管線
-- selector / signal / daybrain 為唯讀消費者
-- ================================================================

-- ============================================================
-- FACT / RAW TABLES (spec §5.1–5.6)
-- Lineage 三欄 (source / data_date / freshness) + source_role
-- ============================================================

CREATE TABLE IF NOT EXISTS core.stocks (
    symbol VARCHAR(10) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    market VARCHAR(20) NOT NULL,
    sector VARCHAR(100),
    industry VARCHAR(100),
    security_type VARCHAR(20) NOT NULL,
    listed_date DATE,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS core.daily_prices (
    symbol VARCHAR(10) NOT NULL,
    trade_date DATE NOT NULL,
    open NUMERIC(14,4),
    high NUMERIC(14,4),
    low NUMERIC(14,4),
    close NUMERIC(14,4),
    adjusted_close NUMERIC(14,4),
    volume BIGINT,
    turnover NUMERIC(20,2),

    source VARCHAR(100),
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    PRIMARY KEY(symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS core.financials (
    symbol VARCHAR(10) NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_quarter INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,

    revenue NUMERIC(20,4),
    gross_profit NUMERIC(20,4),
    operating_income NUMERIC(20,4),
    net_income NUMERIC(20,4),

    eps NUMERIC(14,4),
    book_value_per_share NUMERIC(14,4),

    total_assets NUMERIC(20,4),
    total_liabilities NUMERIC(20,4),
    equity NUMERIC(20,4),

    roe NUMERIC(10,4),
    roa NUMERIC(10,4),

    operating_cash_flow NUMERIC(20,4),
    investing_cash_flow NUMERIC(20,4),
    capex NUMERIC(20,4),
    free_cash_flow NUMERIC(20,4),

    reported_at DATE NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    source VARCHAR(100),
    source_timestamp TIMESTAMP,
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    PRIMARY KEY(symbol, fiscal_year, fiscal_quarter, revision)
);

CREATE TABLE IF NOT EXISTS core.monthly_revenues (
    symbol VARCHAR(10) NOT NULL,
    year_month DATE NOT NULL,
    revenue NUMERIC(20,4),
    yoy_growth NUMERIC(10,4),
    mom_growth NUMERIC(10,4),
    cumulative_revenue NUMERIC(20,4),
    reported_at DATE,
    observed_at TIMESTAMP NOT NULL,
    source VARCHAR(100),
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    PRIMARY KEY(symbol, year_month)
);

CREATE TABLE IF NOT EXISTS core.dividends (
    symbol VARCHAR(10) NOT NULL,
    fiscal_year INTEGER NOT NULL,

    cash_dividend NUMERIC(14,4),
    stock_dividend NUMERIC(14,4),
    payout_ratio NUMERIC(10,4),

    ex_date DATE,
    payment_date DATE,

    source VARCHAR(100),
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    PRIMARY KEY(symbol, fiscal_year)
);

CREATE TABLE IF NOT EXISTS core.institutional_flow (
    symbol VARCHAR(10) NOT NULL,
    trade_date DATE NOT NULL,

    foreign_net BIGINT,
    investment_trust_net BIGINT,
    dealer_net BIGINT,
    total_net BIGINT,

    availability_date DATE NOT NULL,

    source VARCHAR(100),
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    PRIMARY KEY(symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS core.market_context (
    context_type VARCHAR(30) NOT NULL,
    symbol VARCHAR(30) NOT NULL,
    trade_date DATE NOT NULL,

    close NUMERIC(20,4),
    change NUMERIC(20,4),
    change_percent NUMERIC(10,4),

    call_volume BIGINT,
    put_volume BIGINT,
    call_oi BIGINT,
    put_oi BIGINT,
    volume_ratio NUMERIC(10,4),
    oi_ratio NUMERIC(10,4),

    contract VARCHAR(20),
    contract_month VARCHAR(10),
    session VARCHAR(10),
    open NUMERIC(20,4),
    high NUMERIC(20,4),
    low NUMERIC(20,4),
    volume BIGINT,
    settlement NUMERIC(20,4),
    open_interest BIGINT,

    unit VARCHAR(10),

    payload JSONB,

    source VARCHAR(100),
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    observed_at TIMESTAMP NOT NULL DEFAULT NOW(),

    PRIMARY KEY(context_type, symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS core.universe_flags (
    symbol VARCHAR(10) NOT NULL,
    flag_date DATE NOT NULL,

    attention BOOLEAN DEFAULT FALSE,
    disposition BOOLEAN DEFAULT FALSE,
    disposition_reason TEXT,
    suspended BOOLEAN DEFAULT FALSE,

    PRIMARY KEY(symbol, flag_date)
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_core_daily_prices_trade_date ON core.daily_prices(trade_date);
CREATE INDEX IF NOT EXISTS idx_core_daily_prices_symbol ON core.daily_prices(symbol);
CREATE INDEX IF NOT EXISTS idx_core_financials_symbol ON core.financials(symbol);
CREATE INDEX IF NOT EXISTS idx_core_monthly_revenues_symbol ON core.monthly_revenues(symbol);
CREATE INDEX IF NOT EXISTS idx_core_institutional_flow_trade_date ON core.institutional_flow(trade_date);
CREATE INDEX IF NOT EXISTS idx_core_universe_flags_flag_date ON core.universe_flags(flag_date);
CREATE INDEX IF NOT EXISTS idx_core_market_context_trade_date ON core.market_context(trade_date);
CREATE INDEX IF NOT EXISTS idx_core_market_context_type_symbol ON core.market_context(context_type, symbol);

-- ============================================================
-- CONSTRAINTS (source_role check, spec §8.1)
-- ============================================================
-- CONSTRAINTS (source_role check, spec §8.1)
-- Idempotent: uses DO blocks to check constraint existence first
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_core_daily_prices_source_role'
    ) THEN
        ALTER TABLE core.daily_prices
        ADD CONSTRAINT chk_core_daily_prices_source_role
        CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_core_financials_source_role'
    ) THEN
        ALTER TABLE core.financials
        ADD CONSTRAINT chk_core_financials_source_role
        CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_core_monthly_revenues_source_role'
    ) THEN
        ALTER TABLE core.monthly_revenues
        ADD CONSTRAINT chk_core_monthly_revenues_source_role
        CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_core_dividends_source_role'
    ) THEN
        ALTER TABLE core.dividends
        ADD CONSTRAINT chk_core_dividends_source_role
        CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_core_institutional_flow_source_role'
    ) THEN
        ALTER TABLE core.institutional_flow
        ADD CONSTRAINT chk_core_institutional_flow_source_role
        CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_core_market_context_source_role'
    ) THEN
        ALTER TABLE core.market_context
        ADD CONSTRAINT chk_core_market_context_source_role
        CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'));
    END IF;
END $$;
