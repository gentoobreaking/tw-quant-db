# tw-quant-db Entity Relationship Diagram

## Overview

The `twquant_shared` PostgreSQL database uses a **schema-per-project** architecture with a shared `core` schema for canonical data. Below is the entity relationship diagram showing the core schema tables and their relationships to the `public` (gold-analysis) schema.

## Entity Relationship Diagram

```mermaid
erDiagram
    %% core.stocks
    STOCKS {
        symbol PK
        name
        market
        sector
        industry
        security_type
        listed_date
        active
        needs_manual_review
        created_at
        updated_at
    }

    %% core.trading_calendar (backfill 缺口偵測)
    TRADING_CALENDAR {
        trade_date PK
        is_trading
        day_of_week
    }

    %% core.daily_prices
    DAILY_PRICES {
        symbol PK
        trade_date PK
        open
        high
        low
        close
        adjusted_close
        volume
        turnover
        source
        data_date
        freshness
        source_role
    }

    %% core.financials
    FINANCIALS {
        symbol PK
        fiscal_year PK
        fiscal_quarter PK
        revision PK
        revenue
        gross_profit
        operating_income
        net_income
        eps
        book_value_per_share
        total_assets
        total_liabilities
        equity
        roe
        roa
        operating_cash_flow
        investing_cash_flow
        capex
        free_cash_flow
        reported_at
        observed_at
        source
        source_timestamp
        data_date
        freshness
        source_role
    }

    %% core.monthly_revenues
    MONTHLY_REVENUES {
        symbol PK
        year_month PK
        revenue
        yoy_growth
        mom_growth
        cumulative_revenue
        reported_at
        observed_at
        source
        data_date
        freshness
        source_role
    }

    %% core.dividends
    DIVIDENDS {
        symbol PK
        fiscal_year PK
        cash_dividend
        stock_dividend
        payout_ratio
        ex_date
        payment_date
        source
        data_date
        freshness
        source_role
    }

    %% core.institutional_flow
    INSTITUTIONAL_FLOW {
        symbol PK
        trade_date PK
        foreign_net
        investment_trust_net
        dealer_net
        total_net
        availability_date
        source
        data_date
        freshness
        source_role
    }

    %% core.market_context
    MARKET_CONTEXT {
        context_type PK
        symbol PK
        trade_date PK
        close
        change
        change_percent
        call_volume
        put_volume
        call_oi
        put_oi
        volume_ratio
        oi_ratio
        contract
        contract_month
        session
        open
        high
        low
        volume
        settlement
        open_interest
        unit
        payload
        source
        data_date
        freshness
        source_role
        observed_at
    }

    %% core.universe_flags
    UNIVERSE_FLAGS {
        symbol PK
        flag_date PK
        attention
        disposition
        disposition_reason
        suspended
    }

    %% core.margin_trading
    MARGIN_TRADING {
        symbol PK
        trade_date PK
        margin_buy
        margin_sell
        margin_balance
        margin_limit
        short_buy
        short_sell
        short_balance
        short_limit
        offset
        source
        data_date
        freshness
        source_role
    }

    %% core.alerts (created T019)
    ALERTS {
        id PK
        user_id FK
        alert_type
        asset
        target_price
        is_active
        created_at
        triggered_at
        extra_data
    }

    %% core.decisions (created T019)
    DECISIONS {
        id PK
        user_id FK
        portfolio_id FK
        decision_type
        source
        asset
        signal_strength
        confidence
        price_target
        stop_loss
        reason_zh
        reason_en
        indicators_snapshot
        analysis_scores
        is_executed
        executed_at
        execution_price
        model_version
        extra_data
        created_at
        updated_at
    }

    %% public schema (gold-analysis)
    USERS {
        id PK
        username
        email
        hashed_password
    }

    PORTFOLIOS {
        id PK
        user_id FK
        name
        description
        initial_capital
        current_value
    }

    PORTFOLIO_HOLDINGS {
        id PK
        portfolio_id FK
        asset_type
        quantity
        avg_cost
        current_price
        market_value
    }

    %% Relationships
    STOCKS ||--o{ DAILY_PRICES : "has prices for"
    STOCKS ||--o{ FINANCIALS : "has financials for"
    STOCKS ||--o{ MONTHLY_REVENUES : "has revenue for"
    STOCKS ||--o{ DIVIDENDS : "has dividends for"
    STOCKS ||--o{ INSTITUTIONAL_FLOW : "has flow for"
    STOCKS ||--o{ MARKET_CONTEXT : "has context for"
    STOCKS ||--o{ UNIVERSE_FLAGS : "has flags for"
    STOCKS ||--o{ MARGIN_TRADING : "has margin data for"

    TRADING_CALENDAR ||--o{ DAILY_PRICES : "defines trading days for backfill"

    DAILY_PRICES }o--|| ALERTS : "triggers alerts for"
    DAILY_PRICES }o--|| DECISIONS : "informs decisions about"

    USERS ||--o{ PORTFOLIOS : "owns"
    PORTFOLIOS ||--o{ PORTFOLIO_HOLDINGS : "contains"
    USERS ||--o{ ALERTS : "receives"
    USERS }o--|| DECISIONS : "makes"
    PORTFOLIOS ||--o{ DECISIONS : "tracked by"
```

## Schema Summary

| Schema | Purpose | Primary Writer | Readers |
|--------|---------|---------------|---------|
| `public` | gold-analysis user portfolios, alerts, decisions | gold-analysis backend | gold-analysis backend |
| `core` | Shared market data (prices, financials, stocks, alerts, decisions) | tw-quant-pickup pipeline | selector, signal, daybrain, gold-analysis |
| `selector` | Selector algorithm results | tw-quant-selector | tw-quant-selector |
| `signal` | Signal generation results | tw-quant-signal | tw-quant-signal |
| `pickup` | Data ingestion tracking | tw-quant-pickup | tw-quant-pickup |
| `audit` | Audit logging | All services | All services |

## Service Account Permissions Matrix

| Role | Login | Schema Access | Table Privileges | Purpose |
|------|-------|--------------|-----------------|---------|
| `twquant` (admin) | Yes | ALL schemas | ALL (superuser) | DBA / Pipeline admin |
| `twquant_readonly` | No | `core`: USAGE | SELECT on all core tables | Base role for signal/selector/daybrain |
| `twquant_core_writer` | No | `core`: USAGE, CREATE | SELECT, INSERT, UPDATE, DELETE | tw-quant-pickup data ingestion |
| `twquant_pickup` | Yes | `core`: USAGE; `pickup`: ALL | `pickup`: ALL; `core`: SELECT, INSERT, UPDATE, DELETE | Data ingestion pipeline |
| `twquant_selector` | Yes | `core`: USAGE; `selector`: ALL | `selector`: ALL; `core`: SELECT (via twquant_readonly) | Signal selection |
| `twquant_signal` | Yes | `core`: USAGE; `signal`: ALL | `signal`: ALL; `core`: SELECT (via twquant_readonly) | Signal generation |
| `twquant_audit_writer` | No | `audit`: USAGE | INSERT, SELECT on audit tables | Audit logging |

## Key Design Decisions

1. **`source` / `data_date` / `freshness` / `source_role` lineage columns**: Every `core` fact table tracks data provenance. The three source roles are:
   - `CANONICAL` — authoritative data source (default)
   - `SEMI_OFFICIAL_REALTIME` — real-time feed, may need verification
   - `FALLBACK` — fallback data when primary source unavailable

2. **Partition strategy**: `core.daily_prices` is partitioned by `trade_date` monthly. Partitioning is deferred until data volume exceeds 1M rows (currently ~10K). BRIN index on `trade_date` added when partitioning is enabled.

3. **Compatibility views**: `core.v_*_stock` and `selector.v_*_stock` views (providing `stock_id` → `symbol` mapping) were dropped in Phase 3 once all consumer code migrated to `symbol` directly.
