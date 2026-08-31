package main

import (
	"context"
	"time"
)

type PriceData struct {
	Symbol    string    `json:"symbol"`
	TradeDate time.Time `json:"trade_date"`
	Open      float64   `json:"open"`
	High      float64   `json:"high"`
	Low       float64   `json:"low"`
	Close     float64   `json:"close"`
	Volume        int64     `json:"volume"`
	AdjustedClose float64   `json:"adj_close"`
}

type Source interface {
	Name() string
	Sources() []Source
	Fetch(ctx context.Context, symbol string, start, end time.Time) ([]PriceData, error)
	Available(ctx context.Context) bool
}

type SourceStats struct {
	Name        string `json:"source"`
	Success     int    `json:"success_count"`
	Failures    int    `json:"failure_count"`
	RowsFetched int    `json:"rows_fetched"`
}

type BackfillReport struct {
	StartDate   string        `json:"start_date"`
	EndDate     string        `json:"end_date"`
	TotalStocks int           `json:"total_stocks"`
	TotalRows   int           `json:"total_rows"`
	Sources     []SourceStats `json:"sources"`
	Completion  float64       `json:"completion_pct"`
}
