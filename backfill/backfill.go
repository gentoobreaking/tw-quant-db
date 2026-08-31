package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"flag"
	"fmt"
	"math/rand/v2"
	"os"
	"strings"
	"sync"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
	_ "modernc.org/sqlite"
)
var checkpointFilename = "backfill_checkpoint.json"

const (
	qualityThreshold      = 0.7
	maxRetries            = 2
	rateLimitRetryDelay   = 60 * time.Second
	connectionRetryDelay  = 10 * time.Second
	minInterBatchDelay    = 2 * time.Second
	maxInterBatchDelay    = 5 * time.Second
	maxConcurrentStocks   = 10
)

// sourceQuality weights per spec §4 (Table: 1.0, 0.9, 0.7, 0.5).
var sourceQuality = map[string]float64{
	"local-mcp":   1.0,
	"twse-mcp":    0.9,
	"finmind-mcp": 0.7,
	"yfinance-mcp": 0.5,
}

// SourceResult wraps fetched data with a quality score.
type SourceResult struct {
	name      string
	data      []PriceData
	score     float64
	coverage  float64
}

// BackfillOptions configures a single backfill run.
type BackfillOptions struct {
	StartDate  time.Time
	EndDate    time.Time
	StockIDs   []string
	DryRun     bool
	Strategy   string // "monthly" | "auto"
	Range      string // e.g. "5Y", "3M"
	Sources    string // "mcp" | "http" | "both"
	Resume     bool
}


// MonthInterval is a date range bounded by month boundaries.
type MonthInterval struct {
	Start time.Time
	End   time.Time
}

// checkpoint tracks progress for resume.
type checkpoint struct {
	LastStock string    `json:"last_stock"`
	LastMonth string    `json:"last_month"`
}

func recordSourceStats(m map[string]*SourceStats, name string, success, fail int) {
	s, ok := m[name]
	if !ok {
		s = &SourceStats{Name: name}
		m[name] = s
	}
	s.Success += success
	s.Failures += fail
}

// generateMonthlyIntervals returns month intervals from start to end (forward order).
func generateMonthlyIntervals(start, end time.Time) []MonthInterval {
	var intervals []MonthInterval
	cur := time.Date(start.Year(), start.Month(), 1, 0, 0, 0, 0, start.Location())
	endMonth := time.Date(end.Year(), end.Month(), 1, 0, 0, 0, 0, end.Location())
	for cur.Before(endMonth) || cur.Equal(endMonth) {
		monthEnd := cur.AddDate(0, 1, -1)
		if monthEnd.After(end) {
			monthEnd = end
		}
		intervals = append(intervals, MonthInterval{Start: cur, End: monthEnd})
		cur = cur.AddDate(0, 1, 0)
	}
	return intervals
}

// splitIntoBatches splits [start, end] into at most batchSize-day chunks.
func splitIntoBatches(start, end time.Time, batchSize int) []MonthInterval {
	var batches []MonthInterval
	cur := start
	for cur.Before(end) {
		batchEnd := cur.AddDate(0, 0, batchSize-1)
		if batchEnd.After(end) {
			batchEnd = end
		}
		batches = append(batches, MonthInterval{Start: cur, End: batchEnd})
		cur = cur.AddDate(0, 0, batchSize)
	}
	return batches
}

// randomDelay returns a random duration in [min, max).
func randomDelay(min, max time.Duration) time.Duration {
	return time.Duration(rand.Int64N(int64(max-min))) + min
}

func resolveRange(rangeStr string) (time.Time, time.Time) {
	now := time.Now()
	if rangeStr == "" {
		return now.AddDate(-5, 0, 0), now
	}
	unit := rangeStr[len(rangeStr)-1:]
	numStr := rangeStr[:len(rangeStr)-1]
	var num int
	fmt.Sscanf(numStr, "%d", &num)
	switch unit {
	case "y", "Y":
		return now.AddDate(-num, 0, 0), now
	case "m", "M":
		return now.AddDate(0, -num, 0), now
	case "w", "W":
		return now.AddDate(0, 0, -num*7), now
	case "d", "D":
		return now.AddDate(0, 0, -num), now
	default:
		return now.AddDate(-5, 0, 0), now
	}
}

// driverByDSN detects SQL dialect from DATABASE_URL / TW_QUANT_DB_PATH env vars.
func driverByDSN() (string, bool) {
	if dsn := os.Getenv("DATABASE_URL"); dsn != "" {
		return "pgx", true
	}
	if sqlite := os.Getenv("TW_QUANT_DB_PATH"); sqlite != "" {
		return "sqlite", true
	}
	return "", false
}
// getMissingDates queries core.trading_calendar (or weekend fallback) for missing dates per spec §4.
// Supports both PostgreSQL and SQLite (TW_QUANT_DB_PATH) dialects.
func getMissingDates(ctx context.Context, db *sql.DB, symbol string, start, end time.Time) ([]time.Time, error) {
	// Detect SQLite dialect from env (TW_QUANT_DB_PATH overrides DATABASE_URL).
	isSQLite := os.Getenv("TW_QUANT_DB_PATH") != ""

	var query string
	var rows *sql.Rows
	var err error

	if isSQLite {
		query = `
WITH RECURSIVE date_series(d) AS (
    VALUES (date(?1))
  UNION ALL
    SELECT date(d, '+1 day') FROM date_series WHERE d < date(?2)
)
SELECT ds.d AS missing_date
FROM date_series ds
LEFT JOIN core_daily_prices dp
  ON dp.symbol = ?3 AND dp.trade_date = ds.d
LEFT JOIN core_trading_calendar tc
  ON tc.trade_date = ds.d
WHERE dp.trade_date IS NULL
  AND COALESCE(tc.is_trading, CAST(strftime('%w', ds.d) AS INTEGER) NOT IN (0, 6)) = 1
`
		rows, err = db.QueryContext(ctx, query, start.Format("2006-01-02"), end.Format("2006-01-02"), symbol)
	} else {
	query = `
WITH RECURSIVE date_series(d) AS (
    VALUES ($1::date)
  UNION ALL
    SELECT (d + INTERVAL '1 day')::date FROM date_series WHERE d < $2::date
)
SELECT ds.d AS missing_date
FROM date_series ds
LEFT JOIN core.daily_prices dp
  ON dp.symbol = $3 AND dp.trade_date = ds.d
LEFT JOIN core.trading_calendar tc
  ON tc.trade_date = ds.d
WHERE dp.trade_date IS NULL
  AND COALESCE(tc.is_trading, EXTRACT(DOW FROM ds.d) NOT IN (0, 6)) = TRUE
`
		rows, err = db.QueryContext(ctx, query, start, end, symbol)
	}
	if err != nil {
		return nil, fmt.Errorf("missing dates query: %w", err)
	}
	defer rows.Close()
	var missing []time.Time
	for rows.Next() {
		if isSQLite {
			var ds string
			if err := rows.Scan(&ds); err != nil {
				return nil, err
			}
			t, err := time.Parse("2006-01-02", ds)
			if err != nil {
				return nil, err
			}
			missing = append(missing, t)
		} else {
			var d time.Time
			if err := rows.Scan(&d); err != nil {
				return nil, err
			}
			missing = append(missing, d)
		}
	}
	return missing, rows.Err()
}

// markNeedsReview flags a stock for manual review per spec §12 acceptance.
func markNeedsReview(ctx context.Context, db *sql.DB, symbol string) error {
	if os.Getenv("TW_QUANT_DB_PATH") != "" {
		_, err := db.ExecContext(ctx, `UPDATE core_stocks SET needs_manual_review = TRUE WHERE symbol = ?`, symbol)
		return err
	}
	_, err := db.ExecContext(ctx, `UPDATE core.stocks SET needs_manual_review = TRUE WHERE symbol = $1`, symbol)
	return err
}

// upsertPrices writes price data to core.daily_prices with ON CONFLICT DO UPDATE per spec §7.
func upsertPrices(ctx context.Context, db *sql.DB, symbol string, rows []PriceData) (int, error) {
	if len(rows) == 0 {
		return 0, nil
	}
	isSQLite := os.Getenv("TW_QUANT_DB_PATH") != ""
	const batchSize = 200
	var inserted int
	for i := 0; i < len(rows); i += batchSize {
		end := i + batchSize
		if end > len(rows) {
			end = len(rows)
		}
		batch := rows[i:end]
		for _, r := range batch {
			var err error
			if isSQLite {
				_, err = db.ExecContext(ctx, `
INSERT INTO core_daily_prices
  (symbol, trade_date, open, high, low, close, volume, adjusted_close,
   source, data_date, freshness, source_role)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'backfill_go', date('now'), 'FALLBACK', 'FALLBACK')
`, symbol, r.TradeDate, r.Open, r.High, r.Low, r.Close, r.Volume, r.AdjustedClose)
				if err != nil {
					_, err = db.ExecContext(ctx, `
UPDATE core_daily_prices SET open=?, high=?, low=?, close=?, volume=?, adjusted_close=?
WHERE symbol=? AND trade_date=?`, r.Open, r.High, r.Low, r.Close, r.Volume, r.AdjustedClose, symbol, r.TradeDate)
				}
			} else {
				_, err = db.ExecContext(ctx, `
INSERT INTO core.daily_prices
  (symbol, trade_date, open, high, low, close, volume, adjusted_close,
   source, data_date, freshness, source_role)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'backfill_go', CURRENT_DATE, 'FALLBACK', 'FALLBACK')
`, symbol, r.TradeDate, r.Open, r.High, r.Low, r.Close, r.Volume, r.AdjustedClose)
				if err != nil {
					_, err = db.ExecContext(ctx, `
UPDATE core.daily_prices SET open=$3, high=$4, low=$5, close=$6, volume=$7, adjusted_close=$8
WHERE symbol=$1 AND trade_date=$2`, symbol, r.TradeDate, r.Open, r.High, r.Low, r.Close, r.Volume, r.AdjustedClose)
				}
			}
			if err != nil {
				fmt.Fprintf(os.Stderr, "row upsert failed for %s %s: %v\n", symbol, r.TradeDate.Format("2006-01-02"), err)
				continue
			}
			inserted++
		}
	}
	return inserted, nil
}

// fetchWithQuality attempts fetch, computes coverage score per spec §5.2.
func fetchWithQuality(ctx context.Context, s Source, symbol string, start, end time.Time, requested int) (*SourceResult, error) {
	prices, err := s.Fetch(ctx, symbol, start, end)
	if err != nil {
		return nil, err
	}
	if len(prices) == 0 {
		return &SourceResult{name: s.Name(), data: prices, score: 0, coverage: 0}, fmt.Errorf("no data returned")
	}
	coverage := 1.0
	if requested > 0 {
		coverage = float64(len(prices)) / float64(requested)
	}
	weight := sourceQuality[s.Name()]
	score := weight * coverage
	return &SourceResult{name: s.Name(), data: prices, score: score, coverage: coverage}, nil
}

// isRetryable classifies errors per spec §5.3 Switch Triggers.
func isRetryable(err error) bool {
	msg := err.Error()
	return strings.Contains(msg, "rate_limit") ||
		strings.Contains(msg, "deadline") ||
		strings.Contains(msg, "connection") ||
		strings.Contains(msg, "RateLimitExceeded")
}

// fetchWithFallback tries sources in priority order with retry/backoff per spec §5.3.
func fetchWithFallback(ctx context.Context, chain Source, symbol string, start, end time.Time, requested int) (*SourceResult, error) {
	sources := chain.Sources()
	var lastErr error
	for _, s := range sources {
		if !s.Available(ctx) {
			fmt.Fprintf(os.Stderr, "[WARN] source %s unavailable, skipping\n", s.Name())
			continue
		}
		var result *SourceResult
		for attempt := 0; attempt <= maxRetries; attempt++ {
			result, lastErr = fetchWithQuality(ctx, s, symbol, start, end, requested)
			if lastErr == nil && result.coverage >= qualityThreshold {
				return result, nil
			}
			if lastErr != nil && isRetryable(lastErr) {
				if attempt < maxRetries {
					// Exponential backoff: 60s, 120s, 180s (spec §12)
					delay := rateLimitRetryDelay * time.Duration(1<<attempt)
					fmt.Fprintf(os.Stderr, "[WARN] %s retryable error, attempt %d: %v, backing off %v\n", s.Name(), attempt+1, lastErr, delay)
					select {
					case <-time.After(delay):
					case <-ctx.Done():
						return nil, ctx.Err()
					}
					continue
				}
			}
		}
		// Source exhausted — log switch reason.
		if result != nil {
			fmt.Fprintf(os.Stderr, "[WARN] source %s incomplete (coverage=%.2f), switching next source for %s\n", s.Name(), result.coverage, symbol)
		} else if lastErr != nil {
			fmt.Fprintf(os.Stderr, "[WARN] source %s failed: %v, switching next source for %s\n", s.Name(), lastErr, symbol)
		}
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, fmt.Errorf("all sources failed for %s", symbol)
}

// loadStockList resolves stock list from env vars / CLI per spec §3.
func loadStockList(ctx context.Context, db *sql.DB, opts *BackfillOptions) ([]string, error) {
	if opts.StockIDs != nil && len(opts.StockIDs) > 0 {
		return opts.StockIDs, nil
	}
	if f := os.Getenv("STOCKS_FILE"); f != "" {
		data, err := os.ReadFile(f)
		if err != nil {
			return nil, fmt.Errorf("read stocks file: %w", err)
		}
		var ids []string
		for _, line := range strings.Split(string(data), "\n") {
			line = strings.TrimSpace(line)
			if line != "" && !strings.HasPrefix(line, "#") {
				ids = append(ids, line)
			}
		}
		return ids, nil
	}
	if os.Getenv("BACKFILL_ALL_LISTED") == "true" {
		query := "SELECT symbol FROM core.stocks WHERE active = TRUE"
		if os.Getenv("TW_QUANT_DB_PATH") != "" {
			query = "SELECT symbol FROM core_stocks WHERE active = 1"
		}
		rows, err := db.QueryContext(ctx, query)
		if err != nil {
			return nil, fmt.Errorf("fetch stock list: %w", err)
		}
		defer rows.Close()
		var ids []string
		for rows.Next() {
			var s string
			if err := rows.Scan(&s); err != nil {
				return nil, err
			}
			ids = append(ids, s)
		}
		return ids, rows.Err()
	}
	return []string{"2330", "0050", "2317"}, nil
}

// saveCheckpoint writes progress to JSON file.
func saveCheckpoint(cp checkpoint) error {
	data, err := json.Marshal(cp)
	if err != nil {
		return err
	}
	return os.WriteFile(checkpointFilename, data, 0644)
}

// loadCheckpoint reads checkpoint from JSON file.
func loadCheckpoint() (*checkpoint, error) {
	data, err := os.ReadFile(checkpointFilename)
	if err != nil {
		return nil, err
	}
	var cp checkpoint
	if err := json.Unmarshal(data, &cp); err != nil {
		return nil, err
	}
	return &cp, nil
}

// runBackfill executes the monthly backfill orchestrator per spec §12.
func runBackfill(ctx context.Context, db *sql.DB, opts BackfillOptions) (*BackfillReport, error) {
	stocks, err := loadStockList(ctx, db, &opts)
	if err != nil {
		return nil, fmt.Errorf("stock list: %w", err)
	}

	chain := FallbackChain([]Source{
		&LocalMCPSource{addr: os.Getenv("MCP_HOST")},
		&TWSEMCPSource{addr: os.Getenv("TWSE_MCP_HOST")},
		&FinMindMCPSource{addr: os.Getenv("FINMIND_MCP_HOST"), apiKey: os.Getenv("FINMIND_API_KEY")},
		&YFinanceMCPSource{addr: os.Getenv("YFINANCE_MCP_HOST")},
	})

	start, end := opts.StartDate, opts.EndDate
	if start.IsZero() || end.IsZero() {
		start, end = resolveRange(opts.Range)
	}

	intervals := generateMonthlyIntervals(start, end)

	// Load checkpoint for resume.
	var cp *checkpoint
	if opts.Resume {
		cp, err = loadCheckpoint()
		if err != nil {
			fmt.Fprintf(os.Stderr, "[WARN] no checkpoint found, starting fresh: %v\n", err)
			cp = &checkpoint{}
		}
	}

	statsMap := make(map[string]*SourceStats)
	var totalRows int
	var totalSuccess, totalFail int

	var mu sync.Mutex
	var wg sync.WaitGroup
	sem := make(chan struct{}, maxConcurrentStocks)

	for _, month := range intervals {
		monthKey := month.Start.Format("2006-01")
		if opts.Resume && cp != nil && (cp.LastMonth == monthKey) {
			fmt.Fprintf(os.Stderr, "[INFO] skipping completed month %s (checkpoint)\n", monthKey)
			continue
		}

		batches := splitIntoBatches(month.Start, month.End, 5)
		for _, stock := range stocks {
			if opts.Resume && cp != nil && cp.LastStock == stock && cp.LastMonth == monthKey {
				fmt.Fprintf(os.Stderr, "[INFO] resuming from stock %s month %s\n", stock, monthKey)
			}

			for _, batch := range batches {
				missing, err := getMissingDates(ctx, db, stock, batch.Start, batch.End)
				if err != nil {
					totalFail++
					fmt.Fprintf(os.Stderr, "[ERROR] missing date detection failed for %s: %v\n", stock, err)
					continue
				}
				if len(missing) == 0 {
					continue
				}

				delay := randomDelay(minInterBatchDelay, maxInterBatchDelay)
				<-time.After(delay)

				wg.Add(1)
				sem <- struct{}{}
				go func(s string, m MonthInterval, b MonthInterval, miss []time.Time) {
					defer wg.Done()
					defer func() { <-sem }()

					result, ferr := fetchWithFallback(ctx, chain, s, b.Start, b.End, len(miss))
					mu.Lock()
					defer mu.Unlock()
					if ferr != nil {
						totalFail++
						recordSourceStats(statsMap, "fallback-chain", 0, 1)
						if !opts.DryRun {
							if merr := markNeedsReview(ctx, db, s); merr != nil {
								fmt.Fprintf(os.Stderr, "[WARN] markNeedsReview failed for %s: %v\n", s, merr)
							}
						}
						fmt.Fprintf(os.Stderr, "[ERROR] all fallback sources failed for %s %s: %v\n", s, monthKey, ferr)
						return
					}
					totalSuccess++
					totalRows += len(result.data)
					recordSourceStats(statsMap, result.name, 1, 0)
					statsMap[result.name].RowsFetched += len(result.data)
					fmt.Fprintf(os.Stderr, "[INFO] fetched %s from %s for %s (%s), rows=%d, coverage=%.2f\n",
						s, result.name, b.Start.Format("2006-01-02"), b.End.Format("2006-01-02"), len(result.data), result.coverage)

					if !opts.DryRun {
						n, uerr := upsertPrices(ctx, db, s, result.data)
						if uerr != nil {
							fmt.Fprintf(os.Stderr, "[ERROR] upsert failed for %s: %v\n", s, uerr)
						}
						totalRows = totalRows - len(result.data) + n
					}
				}(stock, month, batch, missing)
			}
		}
		wg.Wait()

		// Save checkpoint after each month.
		if !opts.DryRun {
			if err := saveCheckpoint(checkpoint{LastStock: stocks[len(stocks)-1], LastMonth: monthKey}); err != nil {
				fmt.Fprintf(os.Stderr, "[WARN] checkpoint save failed: %v\n", err)
			}
		}
	}

	completion := 0.0
	if totalSuccess+totalFail > 0 {
		completion = float64(totalSuccess) / float64(totalSuccess+totalFail) * 100
	}

	flatStats := make([]SourceStats, 0, len(statsMap))
	for _, s := range statsMap {
		flatStats = append(flatStats, *s)
	}

	report := &BackfillReport{
		StartDate:   start.Format("2006-01-02"),
		EndDate:     end.Format("2006-01-02"),
		TotalStocks: len(stocks),
		TotalRows:   totalRows,
		Sources:     flatStats,
		Completion:  completion,
	}

	if opts.DryRun {
		fmt.Fprintf(os.Stderr, "[INFO] dry run mode — no data persisted\n")
	} else {
		fmt.Fprintf(os.Stderr, "[INFO] backfill complete: %d rows, %.1f%% completion\n", totalRows, completion)
	}
	return report, nil
}

func main() {
	var (
		startDateStr = flag.String("start", "", "Start date (YYYY-MM-DD)")
		endDateStr   = flag.String("end", "", "End date (YYYY-MM-DD)")
		stockIDs     = flag.String("stock-ids", "", "Comma-separated stock IDs")
		rangeStr     = flag.String("range", "5Y", "Range (5Y, 3M, etc.)")
		strategy     = flag.String("strategy", "monthly", "Batch strategy (monthly, auto)")
		sources      = flag.String("sources", "both", "Source mode (mcp, http, both)")
		manualStock  = flag.String("stock", "", "Single stock symbol override")
		dryRun       = flag.Bool("dry-run", false, "Dry run mode — no writes")
		resume       = flag.Bool("resume", false, "Resume from checkpoint")
	)
	flag.Parse()

	var start, end time.Time
	if *startDateStr != "" {
		start, _ = time.Parse("2006-01-02", *startDateStr)
	}
	if *endDateStr != "" {
		end, _ = time.Parse("2006-01-02", *endDateStr)
	}

	var ids []string
	if *stockIDs != "" {
		for _, s := range strings.Split(*stockIDs, ",") {
			s = strings.TrimSpace(s)
			if s != "" {
				ids = append(ids, s)
			}
		}
	}
	if *manualStock != "" {
		ids = []string{*manualStock}
	}

	opts := BackfillOptions{
		StartDate: start,
		EndDate:   end,
		DryRun:    *dryRun,
		Strategy:  *strategy,
		Range:     *rangeStr,
		StockIDs:  ids,
		Sources:   *sources,
		Resume:    *resume,
	}

	if *sources != "mcp" && *sources != "http" && *sources != "both" {
		fmt.Fprintf(os.Stderr, "--sources must be mcp, http, or both (got: %s)\n", *sources)
		os.Exit(1)
	}

	// Resolve DB driver: DATABASE_URL (pgx) or TW_QUANT_DB_PATH (sqlite) per spec §10.
	dsn := os.Getenv("DATABASE_URL")
	var dbDriver string
	if dsn == "" {
		if sqlitePath := os.Getenv("TW_QUANT_DB_PATH"); sqlitePath != "" {
			dbDriver = "sqlite"
			dsn = sqlitePath
		} else {
			fmt.Fprintf(os.Stderr, "DATABASE_URL not set and TW_QUANT_DB_PATH not set\n")
			os.Exit(1)
		}
	} else {
		dbDriver = "pgx"
	}
	db, err := sql.Open(dbDriver, dsn)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open db: %v\n", err)
		os.Exit(1)
	}
	defer db.Close()

	ctx := context.Background()
	if dbDriver == "sqlite" {
		// Auto-init SQLite schema minimal tables (spec §10).
		for _, stmt := range []string{
			`CREATE TABLE IF NOT EXISTS core_stocks (
				symbol VARCHAR(10) PRIMARY KEY,
				name VARCHAR(100) NOT NULL,
				market VARCHAR(20) NOT NULL,
				sector VARCHAR(50),
				active BOOLEAN DEFAULT 1,
				needs_manual_review BOOLEAN DEFAULT 0,
				last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
			)`,
			`CREATE TABLE IF NOT EXISTS core_daily_prices (
				symbol VARCHAR(10) NOT NULL,
				trade_date DATE NOT NULL,
				open REAL,
				high REAL,
				low REAL,
				close REAL,
				volume INTEGER,
				adjusted_close REAL,
				source VARCHAR(50),
				data_date DATE DEFAULT (date('now')),
				freshness INTEGER DEFAULT 0,
				source_role VARCHAR(20) DEFAULT 'FALLBACK',
				PRIMARY KEY (symbol, trade_date)
			)`,
			`CREATE TABLE IF NOT EXISTS core_trading_calendar (
				trade_date DATE NOT NULL PRIMARY KEY,
				is_trading BOOLEAN NOT NULL DEFAULT 1,
				day_of_week INTEGER,
				monthly_return REAL,
				quarterly_return REAL,
				yearly_return REAL
			)`,
			`INSERT OR IGNORE INTO core_stocks (symbol, name, market, active) VALUES ('2330', 'TSMC', 'TWSE', 1)`,
		} {
			if _, err := db.ExecContext(ctx, stmt); err != nil {
				fmt.Fprintf(os.Stderr, "sqlite schema init: %v\n", err)
				os.Exit(1)
			}
		}
	}
	if dbDriver == "pgx" {
		if _, err := db.ExecContext(ctx, "SET search_path TO core, public"); err != nil {
			fmt.Fprintf(os.Stderr, "set search_path: %v\n", err)
			os.Exit(1)
		}
	}

	report, err := runBackfill(ctx, db, opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "backfill: %v\n", err)
		os.Exit(1)
	}
	output, _ := json.MarshalIndent(report, "", "  ")
	_, _ = os.Stderr.WriteString(string(output))
	os.Stderr.WriteString("\n")
	// 持久化彙整報告（各通路筆數/時間範圍/完成率）
	reportDir := "/app/data"
	if _, err := os.Stat(reportDir); os.IsNotExist(err) {
		reportDir = "."
	}
	reportPath := reportDir + "/backfill_report.json"
	// 同時寫 timestamp 檔以保留歷史
	tsPath := fmt.Sprintf("%s/backfill_report_%s.json", reportDir, time.Now().Format("20060102_150405"))
	for _, p := range []string{reportPath, tsPath} {
		if err := os.WriteFile(p, output, 0644); err != nil {
			fmt.Fprintf(os.Stderr, "write report %s failed: %v\n", p, err)
		} else {
			fmt.Fprintf(os.Stderr, "report written to %s\n", p)
		}
	}
}
