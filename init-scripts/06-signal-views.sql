-- T011: Signal schema views mapping core.* tables to signal.* column names
-- Allows tw-quant-signal API to read core tables with SQLite-compatible column names.
-- When DATABASE_URL points to twquant_shared, SignalDB reads via these views.

-- daily_prices: core uses symbol, signal uses stock_id; core uses adjusted_close/turnover, signal uses adj_close/amount
DROP VIEW IF EXISTS signal.daily_prices CASCADE;
DROP TABLE IF EXISTS signal.daily_prices CASCADE;
CREATE OR REPLACE VIEW signal.daily_prices AS
SELECT symbol AS stock_id, trade_date, open, high, low, close, volume,
       turnover AS amount, 1.0 AS adj_factor, adjusted_close AS adj_close
FROM core.daily_prices;

-- dividends: core uses symbol, signal uses stock_id; core uses fiscal_year, signal uses year
DROP VIEW IF EXISTS signal.dividends CASCADE;
DROP TABLE IF EXISTS signal.dividends CASCADE;
CREATE OR REPLACE VIEW signal.dividends AS
SELECT symbol AS stock_id, fiscal_year AS year,
       cash_dividend, stock_dividend
FROM core.dividends;

-- institutional_flows: core.institutional_flow → signal.institutional_flows
-- core: symbol, foreign_net, investment_trust_net, dealer_net, total_net
-- signal: stock_id, foreign_investors_net, sity_investors_net, dealer_net, total_net
DROP VIEW IF EXISTS signal.institutional_flows CASCADE;
DROP TABLE IF EXISTS signal.institutional_flows CASCADE;
CREATE OR REPLACE VIEW signal.institutional_flows AS
SELECT symbol AS stock_id, trade_date, 'TSE' AS market,
       foreign_net AS foreign_investors_net,
       investment_trust_net AS sity_investors_net,
       dealer_net,
       0 AS dealer_proprietary_net,
       0 AS dealer_hedge_net,
       total_net
FROM core.institutional_flow;

COMMENT ON VIEW signal.daily_prices IS 'Read-only view mapping core.daily_prices (symbol→stock_id)';
COMMENT ON VIEW signal.dividends IS 'Read-only view mapping core.dividends (symbol→stock_id, fiscal_year→year)';
COMMENT ON VIEW signal.institutional_flows IS 'Read-only view mapping core.institutional_flow (symbol→stock_id)';
