-- T017: Add core.margin_trading table for TWSE margin/financing data
-- Backfilled from tw-quant-mcp margin dataset (FALLBACK)

CREATE TABLE IF NOT EXISTS core.margin_trading (
    symbol VARCHAR(10) NOT NULL,
    trade_date DATE NOT NULL,

    margin_buy BIGINT,
    margin_sell BIGINT,
    margin_balance BIGINT,
    margin_limit BIGINT,
    short_buy BIGINT,
    short_sell BIGINT,
    short_balance BIGINT,
    short_limit BIGINT,
    "offset" BIGINT,

    source VARCHAR(100),
    data_date DATE,
    freshness VARCHAR(30),
    source_role VARCHAR(30) NOT NULL DEFAULT 'CANONICAL',

    PRIMARY KEY(symbol, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_core_margin_trading_trade_date ON core.margin_trading(trade_date);
CREATE INDEX IF NOT EXISTS idx_core_margin_trading_symbol ON core.margin_trading(symbol);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_core_margin_trading_source_role'
    ) THEN
        ALTER TABLE core.margin_trading
        ADD CONSTRAINT chk_core_margin_trading_source_role
        CHECK (source_role IN ('CANONICAL', 'SEMI_OFFICIAL_REALTIME', 'FALLBACK'));
    END IF;
END $$;