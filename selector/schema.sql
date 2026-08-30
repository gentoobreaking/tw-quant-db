-- selector/schema.sql
-- tw-quant-selector business logic tables in selector schema
-- Core fact tables (stocks, daily_prices, financials, etc.) are in CORE schema
-- selector 只讀 core，業務表存放於 selector schema

CREATE SCHEMA IF NOT EXISTS selector;

-- ============================================================
-- stocks（保留在 selector，供 selector 自己的查詢）
-- Note: core.stocks is the canonical source. selector.stocks 可以為 view。
-- ============================================================

-- ============================================================
-- portfolio（即时持仓）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.portfolio (
    stock_id     VARCHAR(10)   NOT NULL,
    avg_cost     DECIMAL(18,2) NOT NULL,
    shares       INTEGER        NOT NULL,
    is_etf       BOOLEAN       DEFAULT FALSE,
    updated_at   TIMESTAMP      DEFAULT NOW(),
    pl_tsd       DOUBLE PRECISION,
    pl_pct_tsd   DOUBLE PRECISION,
    alert_enabled BOOLEAN       DEFAULT TRUE,
    PRIMARY KEY (stock_id)
);

COMMENT ON TABLE selector.portfolio IS '即时持仓（人工或 API 同步）';

-- ============================================================
-- lots（持仓明细）
-- ============================================================
CREATE SEQUENCE IF NOT EXISTS selector.seq_lots_id;

CREATE TABLE IF NOT EXISTS selector.lots (
    id          VARCHAR(64)   DEFAULT ('lot_' || nextval('selector.seq_lots_id')) NOT NULL,
    stock_id    VARCHAR(10)   NOT NULL,
    date        DATE           NOT NULL,
    shares      INTEGER        NOT NULL,
    cost        DECIMAL(18,2) NOT NULL,
    created_at  TIMESTAMP      DEFAULT NOW(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_lots_stock ON selector.lots(stock_id);

-- ============================================================
-- alert_settings（警示设定）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.alert_settings (
    key          VARCHAR(64)   NOT NULL,
    value        VARCHAR(255),
    is_sensitive BOOLEAN       DEFAULT FALSE,
    updated_at   TIMESTAMP      DEFAULT NOW(),
    PRIMARY KEY (key)
);

-- ============================================================
-- alert_rules（統一規則配置）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.alert_rules (
    rule_name        VARCHAR(100) PRIMARY KEY,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    threshold        DOUBLE PRECISION,
    cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
    severity         VARCHAR(20) NOT NULL DEFAULT 'MEDIUM'
                     CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    description      VARCHAR(255),
    config_json      TEXT DEFAULT '{}',
    message_template TEXT,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled ON selector.alert_rules (enabled);
CREATE INDEX IF NOT EXISTS idx_alert_rules_severity ON selector.alert_rules (severity);

-- ============================================================
-- alert_history（警示发送纪录）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.alert_history (
    log_id          VARCHAR(64)   NOT NULL,
    stock_id        VARCHAR(10),
    triggered_at    TIMESTAMP      DEFAULT NOW(),
    pnl             DECIMAL(18,2),
    pnl_pct         DECIMAL(8,4),
    threshold_type   VARCHAR(20),
    threshold_value  DECIMAL(18,2),
    avg_cost        DECIMAL(18,2),
    current_price   DECIMAL(10,2),
    shares          INTEGER,
    sent            BOOLEAN,
    reason          VARCHAR(255),
    resolved_at     TIMESTAMP NULL,
    resolution_note TEXT NULL,
    PRIMARY KEY (log_id)
);

CREATE INDEX IF NOT EXISTS idx_alert_history_stock ON selector.alert_history(stock_id);
CREATE INDEX IF NOT EXISTS idx_alert_history_triggered ON selector.alert_history(triggered_at);
CREATE INDEX IF NOT EXISTS idx_alert_history_resolved_at ON selector.alert_history(resolved_at);

-- ============================================================
-- alert_cooldowns（執行冷卻追踪）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.alert_cooldowns (
    rule_name    VARCHAR(100)   NOT NULL,
    stock_id     VARCHAR(10),
    last_triggered TIMESTAMP      NOT NULL,
    PRIMARY KEY (rule_name, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_alert_cooldowns_last ON selector.alert_cooldowns(last_triggered);

-- ============================================================
-- realtime_quotes（即时行情）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.realtime_quotes (
    stock_id        VARCHAR(10)   NOT NULL,
    quote_time      TIMESTAMP NOT NULL,
    price           DECIMAL(10, 2),
    volume          BIGINT,
    bid             DECIMAL(10, 2),
    ask             DECIMAL(10, 2),
    change_amt      DECIMAL(10, 2),
    change_pct      DECIMAL(8, 4),
    is_close        BOOLEAN DEFAULT FALSE,
    pe_realtime     DECIMAL(10, 2),
    pb_realtime     DECIMAL(10, 2),
    yield_realtime  DECIMAL(8, 4),
    open_price      DECIMAL(10, 2),
    high_price      DECIMAL(10, 2),
    low_price       DECIMAL(10, 2),
    PRIMARY KEY (stock_id, quote_time)
);

CREATE INDEX IF NOT EXISTS idx_realtime_quotes_time ON selector.realtime_quotes (quote_time);

-- ============================================================
-- intraday_kline（分内 K 線）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.intraday_kline (
    stock_id    VARCHAR(10)   NOT NULL,
    k_time      TIMESTAMP   NOT NULL,
    period_min  INTEGER     NOT NULL DEFAULT 60,
    open        DECIMAL(10, 2),
    high        DECIMAL(10, 2),
    low         DECIMAL(10, 2),
    close       DECIMAL(10, 2),
    volume      BIGINT,
    PRIMARY KEY (stock_id, k_time, period_min)
);

CREATE INDEX IF NOT EXISTS idx_intraday_kline_time ON selector.intraday_kline (k_time);
CREATE INDEX IF NOT EXISTS idx_intraday_kline_stock_time ON selector.intraday_kline (stock_id, k_time);

-- ============================================================
-- backtest_runs（回测运行纪录）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.backtest_runs (
    run_id        VARCHAR(64)   NOT NULL,
    run_at        TIMESTAMP,
    start_date    DATE,
    end_date      DATE,
    strategy_config JSONB,
    total_return  DECIMAL(8,4),
    cagr          DECIMAL(8,4),
    sharpe        DECIMAL(8,4),
    max_drawdown  DECIMAL(8,4),
    calmar        DECIMAL(8,4),
    turnover      DECIMAL(8,4),
    result_path   VARCHAR(255),
    PRIMARY KEY (run_id)
);

-- ============================================================
-- backtest_positions（回测持仓明细）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.backtest_positions (
    run_id     VARCHAR(64)   NOT NULL,
    trade_date DATE           NOT NULL,
    stock_id   VARCHAR(10)   NOT NULL,
    action     VARCHAR(10)   NOT NULL,
    shares     INTEGER,
    price      DECIMAL(10,2),
    value      DECIMAL(18,2),
    weight     DECIMAL(8,4),
    PRIMARY KEY (run_id, trade_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_backtest_positions_run ON selector.backtest_positions(run_id);

-- ============================================================
-- backtest_equity（回测净值曲線）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.backtest_equity (
    run_id          VARCHAR(64)   NOT NULL,
    trade_date      DATE           NOT NULL,
    portfolio_value DECIMAL(18,2),
    benchmark_value DECIMAL(18,2),
    drawdown        DECIMAL(8,4),
    PRIMARY KEY (run_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_run ON selector.backtest_equity(run_id);

-- ============================================================
-- guru_scores（大师策略评分）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.guru_scores (
    score_date      DATE         NOT NULL,
    stock_id        VARCHAR(10) NOT NULL,
    guru            VARCHAR(50) NOT NULL,
    score           DECIMAL(8,4),
    pass_filter     BOOLEAN,
    criteria_detail JSONB,
    PRIMARY KEY (score_date, stock_id, guru)
);

CREATE INDEX IF NOT EXISTS idx_guru_scores_guru ON selector.guru_scores(guru);
CREATE INDEX IF NOT EXISTS idx_guru_scores_date ON selector.guru_scores(score_date);

-- ============================================================
-- strategy_config_history（策略设定历史）
-- ============================================================
CREATE SEQUENCE IF NOT EXISTS selector.seq_strategy_config_history_id;

CREATE TABLE IF NOT EXISTS selector.strategy_config_history (
    config_id        INTEGER   NOT NULL DEFAULT nextval('selector.seq_strategy_config_history_id'),
    changed_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    weights          JSONB,
    advanced_params  JSONB,
    guru_config      JSONB,
    universe_config  JSONB,
    changed_by       VARCHAR(50) DEFAULT 'user',
    note             VARCHAR(255),
    PRIMARY KEY (config_id)
);

CREATE INDEX IF NOT EXISTS idx_strategy_config_history_changed ON selector.strategy_config_history(changed_at);

-- ============================================================
-- ingestion_tracker（资料摄取进度追踪）
-- ============================================================
CREATE TABLE IF NOT EXISTS selector.ingestion_tracker (
    stock_id     VARCHAR(10)   NOT NULL,
    dataset      VARCHAR(50)   NOT NULL,
    bucket       INTEGER,
    last_updated DATE,
    last_status   VARCHAR(20),
    error_msg     VARCHAR(500),
    PRIMARY KEY (stock_id, dataset)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_tracker_dataset ON selector.ingestion_tracker(dataset);
CREATE INDEX IF NOT EXISTS idx_ingestion_tracker_updated ON selector.ingestion_tracker(last_updated);

-- ============================================================
-- operation_logs（操作日志）
-- ============================================================
CREATE SEQUENCE IF NOT EXISTS selector.seq_operation_logs_id;

CREATE TABLE IF NOT EXISTS selector.operation_logs (
    id          VARCHAR(64) DEFAULT ('log_' || nextval('selector.seq_operation_logs_id')) NOT NULL,
    module      VARCHAR(50) NOT NULL,
    event       VARCHAR(100) NOT NULL,
    severity    VARCHAR(20) NOT NULL,
    created_at  TIMESTAMP    DEFAULT NOW(),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_operation_logs_module ON selector.operation_logs(module);
CREATE INDEX IF NOT EXISTS idx_operation_logs_created ON selector.operation_logs(created_at);
