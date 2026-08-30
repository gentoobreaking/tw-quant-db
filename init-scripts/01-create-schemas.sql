-- init-scripts/01-create-schemas.sql
-- Create all 5 schemas for the shared PostgreSQL instance

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS pickup;
CREATE SCHEMA IF NOT EXISTS selector;
CREATE SCHEMA IF NOT EXISTS signal;
CREATE SCHEMA IF NOT EXISTS audit;

COMMENT ON SCHEMA core IS 'Shared raw/fact tables — single source of truth (written by pickup pipeline)';
COMMENT ON SCHEMA pickup IS 'tw-quant-pickup business logic tables (factor_scores, valuations, rankings, etc.)';
COMMENT ON SCHEMA selector IS 'tw-quant-selector business logic tables (portfolio, backtest, alerts, etc.)';
COMMENT ON SCHEMA signal IS 'tw-quant-signal technical indicator & health score tables';
COMMENT ON SCHEMA audit IS 'Shared audit tables (operation_logs, snapshot_audit_log)';
