package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/mark3labs/mcp-go/client"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/client/transport"
)

// mcpEnvelope is the §3.3 Envelope structure returned by tw-quant-mcp.
type mcpEnvelope struct {
	Data      json.RawMessage `json:"data"`
	Lineage   json.RawMessage `json:"_lineage"`
	HTTPCalls int             `json:"http_calls"`
}

// mcpPriceItem is a single price record inside the envelope's data array.
type mcpPriceItem struct {
	Timestamp string  `json:"timestamp"`
	Open      float64 `json:"open"`
	High      float64 `json:"high"`
	Low       float64 `json:"low"`
	Close     float64 `json:"close"`
	Volume    int64   `json:"volume"`
	Amount    float64 `json:"amount"`
}

// parseMCPEnvelope extracts price data from the tw-quant-mcp Envelope response.
func parseMCPEnvelope(text string) ([]PriceData, error) {
	if text == "" {
		return nil, nil
	}
	var env mcpEnvelope
	if err := json.Unmarshal([]byte(text), &env); err != nil {
		return nil, fmt.Errorf("parse mcp envelope: %w", err)
	}
	var items []mcpPriceItem
	if err := json.Unmarshal(env.Data, &items); err != nil {
		return nil, fmt.Errorf("parse mcp data array: %w", err)
	}
	prices := make([]PriceData, 0, len(items))
	for _, item := range items {
		ts, err := time.Parse("2006-01-02", item.Timestamp)
		if err != nil {
			continue
		}
		prices = append(prices, PriceData{
			TradeDate:     ts,
			Open:          item.Open,
			High:          item.High,
			Low:           item.Low,
			Close:         item.Close,
			Volume:        item.Volume,
			AdjustedClose: item.Close,
		})
	}
	return prices, nil
}

// filterByDateRange filters prices to [start, end] inclusive.
func filterByDateRange(prices []PriceData, start, end time.Time) []PriceData {
	var filtered []PriceData
	for _, p := range prices {
		if !p.TradeDate.Before(start) && !p.TradeDate.After(end) {
			filtered = append(filtered, p)
		}
	}
	return filtered
}

// MCPClientWrapper wraps the mcp-go client with lazy initialization.
type MCPClientWrapper struct {
	addr  string   // for HTTP transport
	cmd   string   // for stdio transport
	args  []string // for stdio transport
	env   []string // for stdio transport
	once  bool
	c     *client.Client
}

func (w *MCPClientWrapper) getClient(ctx context.Context) (*client.Client, error) {
	if w.c != nil {
		return w.c, nil
	}
	var c *client.Client
	var err error
	if w.addr != "" {
		c, err = client.NewStreamableHttpClient(w.addr,
			transport.WithHTTPBasicClient(&http.Client{Timeout: 30 * time.Second}),
		)
	} else {
		env := w.env
		if len(env) == 0 {
			env = os.Environ()
		}
		c, err = client.NewStdioMCPClient(w.cmd, env, w.args...)
	}
	if err != nil {
		name := w.cmd
		if name == "" {
			name = w.addr
		}
		return nil, fmt.Errorf("mcp client init for %s: %w", name, err)
	}
	if err := c.Start(ctx); err != nil {
		name := w.cmd
		if name == "" {
			name = w.addr
		}
		return nil, fmt.Errorf("mcp client start failed for %s: %w", name, err)
	}
	initRequest := mcp.InitializeRequest{
		Params: mcp.InitializeParams{
			ProtocolVersion: mcp.LATEST_PROTOCOL_VERSION,
			ClientInfo: mcp.Implementation{
				Name:    "tw-quant-backfill",
				Version: "1.0.0",
			},
			Capabilities: mcp.ClientCapabilities{},
		},
	}
	if _, err := c.Initialize(ctx, initRequest); err != nil {
		name := w.cmd
		if name == "" {
			name = w.addr
		}
		return nil, fmt.Errorf("mcp client initialize failed for %s: %w", name, err)
	}
	w.c = c
	return c, nil
}

// callTool calls get_stock_daily_kline on the MCP server (for local-mcp).
func callTool(ctx context.Context, c *client.Client, symbol string, start, end time.Time) (string, error) {
	request := mcp.CallToolRequest{
		Params: mcp.CallToolParams{
			Name: "get_stock_daily_kline",
			Arguments: map[string]interface{}{
				"symbol": symbol,
				"period": "day",
				"date":   start.Format("2006-01-02"),
			},
		},
	}
	result, err := c.CallTool(ctx, request)
	if err != nil {
		return "", err
	}
	if result == nil || result.IsError {
		return "", fmt.Errorf("tool call returned error: %v", result)
	}
	if result.StructuredContent != nil {
		data, err := json.Marshal(result.StructuredContent)
		if err != nil {
			return "", fmt.Errorf("marshal structuredContent: %w", err)
		}
		return string(data), nil
	}
	var text string
	for _, content := range result.Content {
		if tc, ok := content.(mcp.TextContent); ok {
			text += tc.Text
		}
	}
	return text, nil
}

// callTWSETool calls get_stock_history on TWSE MCP.
func callTWSETool(ctx context.Context, c *client.Client, symbol string, start, end time.Time) (string, error) {
	request := mcp.CallToolRequest{
		Params: mcp.CallToolParams{
			Name: "get_stock_history",
			Arguments: map[string]interface{}{
				"stock_no": symbol,
				"date":     start.Format("20060102"),
			},
		},
	}
	result, err := c.CallTool(ctx, request)
	if err != nil {
		return "", err
	}
	if result == nil || result.IsError {
		return "", fmt.Errorf("tool call returned error: %v", result)
	}
	if result.StructuredContent != nil {
		data, err := json.Marshal(result.StructuredContent)
		if err != nil {
			return "", fmt.Errorf("marshal structuredContent: %w", err)
		}
		return string(data), nil
	}
	var text string
	for _, content := range result.Content {
		if tc, ok := content.(mcp.TextContent); ok {
			text += tc.Text
		}
	}
	return text, nil
}

// parseTWSEEnvelope parses TWSE text table: "日期: 2025-08-01 | 開: 1145.00 | 高: 1150.00 ..."
func parseTWSEEnvelope(text string) ([]PriceData, error) {
	if text == "" {
		return nil, nil
	}
	// Try JSON first (wrapped result)
	var wrapped struct {
		Result string `json:"result"`
	}
	if err := json.Unmarshal([]byte(text), &wrapped); err == nil && wrapped.Result != "" {
		text = wrapped.Result
	}
	var env mcpEnvelope
	if err := json.Unmarshal([]byte(text), &env); err == nil && len(env.Data) > 0 {
		text = string(env.Data)
		// Check if Data is a JSON string containing the table
		var inner string
		if err := json.Unmarshal(env.Data, &inner); err == nil {
			text = inner
		}
	}
	// Parse text table lines
	re := regexp.MustCompile(`日期:\s*(\d{4}-\d{2}-\d{2})\s*\|\s*開:\s*([\d.]+)\s*\|\s*高:\s*([\d.]+)\s*\|\s*低:\s*([\d.]+)\s*\|\s*收:\s*([\d.]+)\s*\|[^|]*成交量:\s*(\d+)`)
	matches := re.FindAllStringSubmatch(text, -1)
	if len(matches) == 0 {
		return nil, fmt.Errorf("no TWSE data found in response")
	}
	prices := make([]PriceData, 0, len(matches))
	for _, m := range matches {
		ts, err := time.Parse("2006-01-02", m[1])
		if err != nil {
			continue
		}
		open, _ := strconv.ParseFloat(m[2], 64)
		high, _ := strconv.ParseFloat(m[3], 64)
		low, _ := strconv.ParseFloat(m[4], 64)
		close, _ := strconv.ParseFloat(m[5], 64)
		volume, _ := strconv.ParseInt(m[6], 10, 64)
		prices = append(prices, PriceData{
			TradeDate:     ts,
			Open:          open,
			High:          high,
			Low:           low,
			Close:         close,
			Volume:        volume,
			AdjustedClose: close,
		})
	}
	return prices, nil
}

// callFinMindTool calls query_dataset on FinMind MCP
func callFinMindTool(ctx context.Context, c *client.Client, symbol string, start, end time.Time) (string, error) {
	request := mcp.CallToolRequest{
		Params: mcp.CallToolParams{
			Name: "query_dataset",
			Arguments: map[string]interface{}{
				"dataset":    "TaiwanStockPrice",
				"data_id":    symbol,
				"start_date": start.Format("2006-01-02"),
				"end_date":   end.Format("2006-01-02"),
			},
		},
	}
	result, err := c.CallTool(ctx, request)
	if err != nil {
		return "", err
	}
	if result == nil || result.IsError {
		return "", fmt.Errorf("tool call returned error: %v", result)
	}
	if result.StructuredContent != nil {
		data, err := json.Marshal(result.StructuredContent)
		if err != nil {
			return "", fmt.Errorf("marshal structuredContent: %w", err)
		}
		return string(data), nil
	}
	var text string
	for _, content := range result.Content {
		if tc, ok := content.(mcp.TextContent); ok {
			text += tc.Text
		}
	}
	return text, nil
}

// parseFinMindEnvelope parses FinMind markdown table
func parseFinMindEnvelope(text string) ([]PriceData, error) {
	if text == "" {
		return nil, nil
	}
	var env mcpEnvelope
	if err := json.Unmarshal([]byte(text), &env); err == nil && len(env.Data) > 0 {
		text = string(env.Data)
		var inner string
		if err := json.Unmarshal(env.Data, &inner); err == nil {
			text = inner
		}
	}
	var wrapped struct {
		Result string `json:"result"`
	}
	if err := json.Unmarshal([]byte(text), &wrapped); err == nil && wrapped.Result != "" {
		text = wrapped.Result
	}
	// Parse markdown table: | 2025-08-25 | 2330 | 27470807 | ... | 1165.0 | 1180.0 | 1160.0 | 1170.0 |
	lines := strings.Split(text, "\n")
	prices := make([]PriceData, 0)
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "|") || strings.Contains(line, "---") || strings.Contains(line, "date") {
			continue
		}
		parts := strings.Split(line, "|")
		// parts[0] is empty before first |, parts[1]=date, [2]=stock_id, [3]=Trading_Volume, [4]=Trading_money, [5]=open, [6]=max, [7]=min, [8]=close
		if len(parts) < 9 {
			continue
		}
		dateStr := strings.TrimSpace(parts[1])
		if dateStr == "" || dateStr == "date" {
			continue
		}
		ts, err := time.Parse("2006-01-02", dateStr)
		if err != nil {
			continue
		}
		volStr := strings.TrimSpace(parts[3])
		volume, _ := strconv.ParseInt(volStr, 10, 64)
		openStr := strings.TrimSpace(parts[5])
		open, _ := strconv.ParseFloat(openStr, 64)
		maxStr := strings.TrimSpace(parts[6])
		high, _ := strconv.ParseFloat(maxStr, 64)
		minStr := strings.TrimSpace(parts[7])
		low, _ := strconv.ParseFloat(minStr, 64)
		closeStr := strings.TrimSpace(parts[8])
		close, _ := strconv.ParseFloat(closeStr, 64)
		prices = append(prices, PriceData{
			TradeDate:     ts,
			Open:          open,
			High:          high,
			Low:           low,
			Close:         close,
			Volume:        volume,
			AdjustedClose: close,
		})
	}
	if len(prices) == 0 {
		return nil, fmt.Errorf("no FinMind data found")
	}
	return prices, nil
}

// --- Source Implementations ---

// LocalMCPSource connects to tw-quant-mcp container via MCP HTTP protocol.
type LocalMCPSource struct {
	addr    string
	wrapper *MCPClientWrapper
}

func (s *LocalMCPSource) Name() string                       { return "local-mcp" }
func (s *LocalMCPSource) Sources() []Source                  { return []Source{s} }
func (s *LocalMCPSource) Available(ctx context.Context) bool {
	c, err := s.getWrapper(ctx)
	if err != nil {
		return false
	}
	return c != nil
}

func (s *LocalMCPSource) getWrapper(ctx context.Context) (*MCPClientWrapper, error) {
	if s.wrapper == nil {
		addr := s.addr
		if addr == "" {
			addr = os.Getenv("MCP_HOST")
		}
		if addr == "" {
			return nil, fmt.Errorf("MCP_HOST not set")
		}
		if !hasMCPPrefix(addr) {
			addr = addr + "/mcp"
		}
		s.wrapper = &MCPClientWrapper{addr: addr}
	}
	return s.wrapper, nil
}

func (s *LocalMCPSource) Fetch(ctx context.Context, symbol string, start, end time.Time) ([]PriceData, error) {
	w, err := s.getWrapper(ctx)
	if err != nil {
		return nil, err
	}
	c, err := w.getClient(ctx)
	if err != nil {
		return nil, err
	}
	text, err := callTool(ctx, c, symbol, start, end)
	if err != nil {
		return nil, err
	}
	prices, err := parseMCPEnvelope(text)
	if err != nil {
		return nil, fmt.Errorf("parse local-mcp response: %w", err)
	}
	filtered := filterByDateRange(prices, start, end)
	return filtered, nil
}

// TWSEMCPSource connects to TWSEMCPServer remote HTTP MCP endpoint.
type TWSEMCPSource struct {
	addr    string
	wrapper *MCPClientWrapper
}

func (s *TWSEMCPSource) Name() string                       { return "twse-mcp" }
func (s *TWSEMCPSource) Sources() []Source                  { return []Source{s} }
func (s *TWSEMCPSource) Available(ctx context.Context) bool {
	c, err := s.getWrapper(ctx)
	if err != nil {
		return false
	}
	return c != nil
}

func (s *TWSEMCPSource) getWrapper(ctx context.Context) (*MCPClientWrapper, error) {
	if s.wrapper == nil {
		addr := s.addr
		if addr == "" {
			addr = os.Getenv("TWSE_MCP_HOST")
			if addr == "" {
				addr = "https://TW-Stock-MCP-Server.fastmcp.app/mcp"
			}
		}
		s.wrapper = &MCPClientWrapper{addr: addr}
	}
	return s.wrapper, nil
}

func (s *TWSEMCPSource) Fetch(ctx context.Context, symbol string, start, end time.Time) ([]PriceData, error) {
	w, err := s.getWrapper(ctx)
	if err != nil {
		return nil, err
	}
	c, err := w.getClient(ctx)
	if err != nil {
		return nil, err
	}
	text, err := callTWSETool(ctx, c, symbol, start, end)
	if err != nil {
		return nil, err
	}
	prices, err := parseTWSEEnvelope(text)
	if err != nil {
		return nil, fmt.Errorf("parse twse-mcp response: %w", err)
	}
	filtered := filterByDateRange(prices, start, end)
	return filtered, nil
}

// FinMindMCPSource fetches from FinMind MCP (stdio transport).
type FinMindMCPSource struct {
	addr    string
	apiKey  string
	wrapper *MCPClientWrapper
}

func (s *FinMindMCPSource) Name() string                       { return "finmind-mcp" }
func (s *FinMindMCPSource) Sources() []Source                  { return []Source{s} }
func (s *FinMindMCPSource) Available(ctx context.Context) bool {
	w, err := s.getWrapper(ctx)
	if err != nil {
		return false
	}
	c, err := w.getClient(ctx)
	return err == nil && c != nil
}

func (s *FinMindMCPSource) getWrapper(ctx context.Context) (*MCPClientWrapper, error) {
	if s.wrapper == nil {
		apiKey := os.Getenv("FINMIND_API_KEY")
		if apiKey == "" {
			apiKey = os.Getenv("FINMIND_TOKEN")
		}
		if apiKey == "" {
			return nil, fmt.Errorf("FINMIND_API_KEY/FINMIND_TOKEN not set")
		}
		s.wrapper = &MCPClientWrapper{
			cmd:  "uvx",
			args: []string{"finmind-mcp"},
			env:  append(os.Environ(), "FINMIND_TOKEN="+apiKey),
		}
	}
	return s.wrapper, nil
}

func (s *FinMindMCPSource) Fetch(ctx context.Context, symbol string, start, end time.Time) ([]PriceData, error) {
	w, err := s.getWrapper(ctx)
	if err != nil {
		return nil, err
	}
	c, err := w.getClient(ctx)
	if err != nil {
		return nil, err
	}
	text, err := callFinMindTool(ctx, c, symbol, start, end)
	if err != nil {
		return nil, err
	}
	prices, err := parseFinMindEnvelope(text)
	if err != nil {
		return nil, fmt.Errorf("parse finmind-mcp response: %w", err)
	}
	filtered := filterByDateRange(prices, start, end)
	return filtered, nil
}

// YFinanceMCPSource fetches from yfinance MCP (stdio transport).
type YFinanceMCPSource struct {
	addr    string
	wrapper *MCPClientWrapper
}

func (s *YFinanceMCPSource) Name() string                       { return "yfinance-mcp" }
func (s *YFinanceMCPSource) Sources() []Source                  { return []Source{s} }
func (s *YFinanceMCPSource) Available(ctx context.Context) bool {
	w, err := s.getWrapper(ctx)
	if err != nil {
		return false
	}
	c, err := w.getClient(ctx)
	return err == nil && c != nil
}

func (s *YFinanceMCPSource) getWrapper(ctx context.Context) (*MCPClientWrapper, error) {
	if s.wrapper == nil {
		s.wrapper = &MCPClientWrapper{
			cmd:  "uvx",
			args: []string{"--with", "mcp<2", "yfinance-mcp"},
			env:  os.Environ(),
		}
	}
	return s.wrapper, nil
}

func (s *YFinanceMCPSource) Fetch(ctx context.Context, symbol string, start, end time.Time) ([]PriceData, error) {
	w, err := s.getWrapper(ctx)
	if err != nil {
		return nil, err
	}
	c, err := w.getClient(ctx)
	if err != nil {
		return nil, err
	}
	yfSymbol := symbol
	if len(symbol) == 4 {
		yfSymbol = symbol + ".TW"
	}
	request := mcp.CallToolRequest{
		Params: mcp.CallToolParams{
			Name: "get_price_history",
			Arguments: map[string]interface{}{
				"symbol":   yfSymbol,
				"start":    start.Format("2006-01-02"),
				"end":      end.Format("2006-01-02"),
				"interval": "1d",
			},
		},
	}
	result, err := c.CallTool(ctx, request)
	if err != nil {
		return nil, err
	}
	if result == nil || result.IsError {
		return nil, fmt.Errorf("tool call returned error: %v", result)
	}
	var text string
	if result.StructuredContent != nil {
		data, err := json.Marshal(result.StructuredContent)
		if err != nil {
			return nil, fmt.Errorf("marshal structuredContent: %w", err)
		}
		text = string(data)
	} else {
		for _, content := range result.Content {
			if tc, ok := content.(mcp.TextContent); ok {
				text += tc.Text
			}
		}
	}
	// yfinance returns JSON array, try generic parsers
	prices, err := parseMCPEnvelope(text)
	if err == nil && len(prices) > 0 {
		filtered := filterByDateRange(prices, start, end)
		return filtered, nil
	}
	prices, err = parseFinMindEnvelope(text)
	if err == nil && len(prices) > 0 {
		filtered := filterByDateRange(prices, start, end)
		return filtered, nil
	}
	return nil, fmt.Errorf("parse yfinance-mcp response: no data")
}

// FallbackChain tries sources in priority order.
func FallbackChain(sources []Source) Source {
	return &chainSource{sources: sources}
}

type chainSource struct {
	sources []Source
}

func (c *chainSource) Name() string                       { return "fallback-chain" }
func (c *chainSource) Sources() []Source                  { return c.sources }
func (c *chainSource) Available(ctx context.Context) bool {
	for _, s := range c.sources {
		if s.Available(ctx) {
			return true
		}
	}
	return false
}

func (c *chainSource) Fetch(ctx context.Context, symbol string, start, end time.Time) ([]PriceData, error) {
	var errs []error
	for _, s := range c.sources {
		if !s.Available(ctx) {
			continue
		}
		prices, err := s.Fetch(ctx, symbol, start, end)
		if err == nil && len(prices) > 0 {
			return prices, nil
		}
		if err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", s.Name(), err))
		}
	}
	return nil, fmt.Errorf("all sources failed: %v", errs)
}

// hasMCPPrefix checks if URL already has /mcp path.
func hasMCPPrefix(url string) bool {
	return bytes.HasSuffix([]byte(url), []byte("/mcp"))
}
