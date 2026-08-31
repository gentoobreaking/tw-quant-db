# tw-quant-db Core Data Backfill Specification

## 1. Goal

Provide an automated, configurable backfill mechanism for `core.daily_prices`
that:

- Detects missing dates per stock within a requested range
- Sources data from a prioritized **fallback chain**: local MCP → TWSE_MCP → FinMind_MCP → yfinance_MCP
- Switches sources automatically based on **quality criteria** (availability, coverage, authority)
- Respects upstream API rate limits through **batched requests**
- Supports flexible **stock selection** via env vars, external file, or all listed stocks
- Is idempotent — re-running does not create duplicate rows

---

## 2. Data Sources (Fallback Chain)

| Rank | Name            | Type         | Quality Weight | Notes                          |
|------|------------------|--------------|----------------|--------------------------------|
| 1    | local-mcp        | Container    | 1.0            | tw-quant-mcp local service     |
| 2    | twse-online      | HTTP API     | 0.9            | Official TWSE source           |
| 3    | finmind-mcp      | HTTP API     | 0.7            | FinMind public data            |
| 4    | yfinance-mcp     | HTTP API     | 0.5            | Yahoo Finance via MCP          |

---

## 3. Stock Selection

### Env Vars (checked in priority order)

| Env Var          | Description                              | Default Behavior                |
|------------------|------------------------------------------|---------------------------------|
| `BACKFILL_ALL_LISTED` | `true` → fetch all TWSE/OTC stocks   | Skips if `STOCK_IDS` or `STOCKS_FILE` set |
| `STOCK_IDS`      | Comma-separated e.g. `2330,0050`         | Use only specified stocks       |
| `STOCKS_FILE`    | Path to file: one stock_id per line      | Read external configuration     |

If none specified, defaults to `["2330", "0050", "2317"]` for testing.

---

## 4. Missing Date Detection

```sql
-- For each stock_id, find dates in [start_date, end_date]
-- that do NOT exist in core.daily_prices
WITH RECURSIVE date_series(d) AS (
    VALUES (%(start_date)s::date)
  UNION ALL
    SELECT d + INTERVAL '1 day' FROM date_series WHERE d < %(end_date)s
)
SELECT ds.d AS missing_date
FROM date_series ds
LEFT JOIN core.daily_prices dp 
  ON dp.stock_id = %(stock_id)s AND dp.trade_date = ds.d
WHERE dp.trade_date IS NULL
  AND EXTRACT(DOW FROM ds.d) NOT IN (0, 6)  -- exclude weekends (basic filter)
```

- Trading calendar logic should come from `core.trading_calendar` if available
- Otherwise, use weekend exclusion as fallback

---

## 5. Fallback Logic

### Source Availability Check
- HTTP ping or MCP status call (timeout = 5s)
- Mark unavailable sources immediately

### Data Completeness Scoring
After fetching from a source:

```
coverage_score = returned_dates_count / requested_dates_count
```

If `coverage_score < 0.7`, mark as `incomplete`.

### Switch Triggers

| Error Type               | Action              | Retry Delay |
|--------------------------|---------------------|-------------|
| `RateLimitExceeded`      | Retry w/ backoff    | 60s         |
| `ConnectionError/Timeout`| Retry up to 2 times | 10s         |
| `NoDataReturned`         | Switch to next      | Immediate   |
| `IncompleteData` (>30% missing)| Switch to next | Immediate   |

### Quality-Based Selection Algorithm

```python
score = source_weight × availability × coverage_score
if score < threshold:
    switch_to_next_source()
```

---

## 6. Batch Strategy

To respect upstream limits:

- **Max batch size**: 5 consecutive trading days
- **Inter-batch delay**: Random 2–5s
- **Per-source daily limit**:
  - local-mcp: unlimited (cached)
  - twse-online: 100 requests/day per IP
  - finmind-mcp: 50 requests/day free tier
  - yfinance-mcp: 30 requests/min sliding window

---

## 7. Upsert Logic

Write into `core.daily_prices`:

```sql
INSERT INTO core.daily_prices 
  (stock_id, trade_date, open, high, low, close, volume, adj_close)
VALUES (%(...))
ON CONFLICT (stock_id, trade_date) 
DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    adj_close = EXCLUDED.adj_close
```

- Uses `ON CONFLICT DO UPDATE` to ensure idempotent writes
- Preserves primary key `(stock_id, trade_date)`

---

## 8. CLI Interface

```bash
# Full auto mode (detect all missing since last update)
python backfill_core.py --auto

# Specific date range  
python backfill_core.py --start 2026-08-25 --end 2026-08-31

# Single stock override
python backfill_core.py --start 2026-08-25 --stock-ids 2330,3008

# Dry-run mode (no writes)
python backfill_core.py --dry-run --start 2026-08-25
```

---

## 9. Docker Integration

```yaml
services:
  backfill:
    build:
      context: .
      dockerfile: Dockerfile.backfill
    container_name: tw-quant-backfill
    environment:
      - DATABASE_URL=postgresql://twquant:<secret>@host.docker.internal:5432/twquant_shared
      - MCP_HOST=http://tw-quant-mcp:8000    # for local-mcp source
      - STOCK_IDS=
      - STOCKS_FILE=
      - BACKFILL_ALL_LISTED=false
    profiles: ["backfill"]
    restart: "no"
```

---

## 10. Acceptance Criteria

- [ ] Missing dates detected accurately per stock
- [ ] Fallback chain tried in order until data is sufficient
- [ ] No writes to stdout during normal operation (only warnings/errors)
- [ ] Idempotent re-runs do not create duplicates
- [ ] Rate limiting handled gracefully (backoff + retry)
- [ ] Works with `TW_QUANT_DB_PATH` (sqlite fallback for dev)
- [ ] Logs every source switch with reason

---

## 11. Out of Scope

- Backfilling `core.financials` (handled by separate pipeline)
- Intraday K-line backfill (will be separate spec)
- User-facing API exposure (internal CLI tool only)
