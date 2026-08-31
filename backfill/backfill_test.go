package main

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"

	_ "modernc.org/sqlite"
)

// stubSource for testing fallback chain behavior.
type stubSource struct {
	name string
	data []PriceData
	err  error
	avail bool
}

func (s *stubSource) Name() string                       { return s.name }
func (s *stubSource) Sources() []Source                  { return []Source{s} }
func (s *stubSource) Available(ctx context.Context) bool { return s.avail }
func (s *stubSource) Fetch(ctx context.Context, symbol string, start, end time.Time) ([]PriceData, error) {
	if s.err != nil {
		return nil, s.err
	}
	return s.data, nil
}

// setupTestDB creates an in-memory sqlite DB with core schema for testing.
func setupTestDB(t *testing.T) *sql.DB {
	t.Helper()
	tmpDir := t.TempDir()
	dbPath := filepath.Join(tmpDir, "test.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}

	// Create core schema tables needed by tests (sqlite-compatible subset).
	schema := []string{
		`CREATE TABLE core_daily_prices (
			symbol TEXT NOT NULL,
			trade_date DATE NOT NULL,
			open REAL, high REAL, low REAL, close REAL,
			volume INTEGER, adjusted_close REAL,
			source TEXT, data_date DATE, freshness TEXT,
			source_role TEXT DEFAULT 'CANONICAL',
			PRIMARY KEY(symbol, trade_date)
		)`,
		`CREATE TABLE core_stocks (
			symbol TEXT PRIMARY KEY,
			name TEXT, market TEXT,
			sector TEXT, industry TEXT, security_type TEXT,
			listed_date DATE, active BOOLEAN DEFAULT TRUE,
			created_at TEXT, updated_at TEXT
		)`,
	}
	// sqlite doesn't support namespaces/schema in CREATE TABLE directly;
	// we use prefixed table names and adapt getMissingDates queries.
	for _, s := range schema {
		if _, err := db.Exec(s); err != nil {
			t.Fatalf("create table: %v", err)
		}
	}
	return db
}

func TestGenerateMonthlyIntervals(t *testing.T) {
	tests := []struct {
		name      string
		start     string
		end       string
		wantCount int
	}{
		{"single month", "2026-08-01", "2026-08-15", 1},
		{"two months", "2026-07-15", "2026-08-15", 2},
		{"five years", "2021-08-01", "2026-08-31", 61}, // spec §12: 5 years × 12 months
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			start, _ := time.Parse("2006-01-02", tt.start)
			end, _ := time.Parse("2006-01-02", tt.end)
			intervals := generateMonthlyIntervals(start, end)
			if len(intervals) != tt.wantCount {
				t.Errorf("got %d intervals, want %d", len(intervals), tt.wantCount)
			}
		})
	}
}

func TestResolveRange(t *testing.T) {
	// --range 5Y
	start, end := resolveRange("5Y")
	if end.Year()-start.Year() != 5 {
		t.Errorf("5Y range should span 5 years, got %d", end.Year()-start.Year())
	}

	// --range 3M
	start3, _ := resolveRange("3M")
	if time.Since(start3) > 95*24*time.Hour || time.Since(start3) < 89*24*time.Hour {
		t.Errorf("3M range should be ~90 days ago, got %v", start3)
	}
}

func TestRandomDelay(t *testing.T) {
	min := 2 * time.Second
	max := 5 * time.Second
	for range 100 {
		d := randomDelay(min, max)
		if d < min || d >= max {
			t.Errorf("delay %v outside [%v, %v)", d, min, max)
		}
	}
}

func TestSplitIntoBatches(t *testing.T) {
	start, _ := time.Parse("2006-01-02", "2026-08-01")
	end, _ := time.Parse("2006-01-02", "2026-08-31")
	batches := splitIntoBatches(start, end, 5)
	// August has 31 days → ceil(30/5) = 6 batches
	if len(batches) != 6 {
		t.Errorf("expected 6 batches (5-day), got %d", len(batches))
	}
	// Each batch ≤ 5 days
	for _, b := range batches {
		days := b.End.Sub(b.Start).Hours() / 24
		if days > 4 { // 5-day batch means End-Start = 4 days
			t.Errorf("batch spans %.0f days, max 4", days)
		}
	}
}

func TestIsRetryable(t *testing.T) {
	retryable := []error{
		context.DeadlineExceeded,
	}
	for _, e := range retryable {
		if !isRetryable(e) {
			t.Errorf("expected %v to be retryable", e)
		}
	}

	nonRetryable := []error{
		errNoData{},
	}
	for _, e := range nonRetryable {
		if isRetryable(e) {
			t.Errorf("expected %v to NOT be retryable", e)
		}
	}
}

type errNoData struct{}

func (errNoData) Error() string { return "no data returned" }

func TestStockListDefaults(t *testing.T) {
	os.Unsetenv("STOCK_IDS")
	os.Unsetenv("STOCKS_FILE")
	os.Unsetenv("BACKFILL_ALL_LISTED")

	db := setupTestDB(t)
	defer db.Close()

	opts := &BackfillOptions{StockIDs: nil}
	stocks, err := loadStockList(context.Background(), db, opts)
	if err != nil {
		t.Fatalf("loadStockList: %v", err)
	}
	// spec §3: default test set
	want := []string{"2330", "0050", "2317"}
	if len(stocks) != len(want) {
		t.Errorf("got %d stocks, want %d", len(stocks), len(want))
	}
}

func TestCheckpointSaveLoad(t *testing.T) {
	// Save checkpoint to a temp dir.
	tmpDir := t.TempDir()
	old := checkpointFilename
	checkpointFilename = filepath.Join(tmpDir, "checkpoint.json")
	defer func() { checkpointFilename = old }()

	cp := checkpoint{LastStock: "2330", LastMonth: "2026-08"}
	if err := saveCheckpoint(cp); err != nil {
		t.Fatalf("saveCheckpoint: %v", err)
	}

	loaded, err := loadCheckpoint()
	if err != nil {
		t.Fatalf("loadCheckpoint: %v", err)
	}
	if loaded.LastStock != "2330" || loaded.LastMonth != "2026-08" {
		t.Errorf("got %+v, want {2330, 2026-08}", loaded)
	}
}

// stubChainSource returns a chain with only a stub source for testing.
func stubChain(stub *stubSource) Source {
	return FallbackChain([]Source{stub})
}

func TestRunBackfillDryRun(t *testing.T) {
	// Test the full dry-run path: runBackfill with a stub source.
	// We can't easily test runBackfill directly because it builds its own chain,
	// but we can verify the components work together via FetchChain.
	stub := &stubSource{
		name: "test-source",
		data: []PriceData{
			{Symbol: "2330", TradeDate: time.Date(2026, 8, 25, 0, 0, 0, 0, time.UTC), Open: 100, High: 105, Low: 98, Close: 103, Volume: 1000, AdjustedClose: 103},
			{Symbol: "2330", TradeDate: time.Date(2026, 8, 26, 0, 0, 0, 0, time.UTC), Open: 103, High: 108, Low: 102, Close: 106, Volume: 1200, AdjustedClose: 106},
		},
		avail: true,
	}
	chain := stubChain(stub)

	ctx := context.Background()
	if !chain.Available(ctx) {
		t.Fatal("stub chain should be available")
	}
	result, err := fetchWithFallback(ctx, chain, "2330",
		time.Date(2026, 8, 25, 0, 0, 0, 0, time.UTC),
		time.Date(2026, 8, 26, 0, 0, 0, 0, time.UTC), 2)
	if err != nil {
		t.Fatalf("fetchWithFallback: %v", err)
	}
	if result.name != "test-source" {
		t.Errorf("got source %s, want test-source", result.name)
	}
	if len(result.data) != 2 {
		t.Errorf("got %d rows, want 2", len(result.data))
	}
	if result.coverage < 1.0 { // 2/2 = 1.0
		t.Errorf("coverage %.2f should be 1.0", result.coverage)
	}
}

func TestUpsertPricesIdempotent(t *testing.T) {
	// Test that upsert is idempotent: second run doesn't duplicate.
	os.Setenv("TW_QUANT_DB_PATH", ":memory:")
	defer os.Unsetenv("TW_QUANT_DB_PATH")

	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()

	// Create schema.
	_, err = db.Exec(`CREATE TABLE core_daily_prices (
		symbol TEXT NOT NULL, trade_date DATE NOT NULL, open REAL, high REAL,
		low REAL, close REAL, volume INTEGER, adjusted_close REAL,
		source TEXT, data_date DATE, freshness TEXT, source_role TEXT DEFAULT 'CANONICAL',
		PRIMARY KEY(symbol, trade_date)
	)`)
	if err != nil {
		t.Fatalf("create table: %v", err)
	}

	rows := []PriceData{
		{Symbol: "2330", TradeDate: time.Date(2026, 8, 25, 0, 0, 0, 0, time.UTC), Open: 100, High: 105, Low: 98, Close: 103, Volume: 1000, AdjustedClose: 103},
		{Symbol: "2330", TradeDate: time.Date(2026, 8, 26, 0, 0, 0, 0, time.UTC), Open: 103, High: 108, Low: 102, Close: 106, Volume: 1200, AdjustedClose: 106},
	}

	// First insert.
	n1, err := upsertPrices(context.Background(), db, "2330", rows)
	if err != nil {
		t.Fatalf("first upsert: %v", err)
	}
	if n1 != 2 {
		t.Errorf("first upsert: got %d rows, want 2", n1)
	}

	// Second insert (idempotent — should update, not duplicate).
	n2, err := upsertPrices(context.Background(), db, "2330", rows)
	if err != nil {
		t.Fatalf("second upsert: %v", err)
	}
	if n2 != 2 {
		t.Errorf("second upsert: got %d rows, want 2", n2)
	}

	// Verify no duplicates.
	var count int
	err = db.QueryRow("SELECT COUNT(*) FROM core_daily_prices").Scan(&count)
	if err != nil {
		t.Fatalf("count: %v", err)
	}
	if count != 2 {
		t.Errorf("after idempotent upsert: got %d rows, want 2 (no duplicates)", count)
	}
}
