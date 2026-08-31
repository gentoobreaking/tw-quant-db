package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/mark3labs/mcp-go/client"
	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/client/transport"
)

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
		transport.WithHTTPBasicClient(&http.Client{Timeout: 10 * time.Second}),
		)
	} else {
		// Stdio transport (finmind-mcp, yfinance-mcp)
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
	if err := c.Ping(ctx); err != nil {
		name := w.cmd
		if name == "" {
			name = w.addr
		}
		return nil, fmt.Errorf("mcp ping failed for %s: %w", name, err)
	}
	w.c = c
	return c, nil
}

// callTool calls an MCP tool and returns the text content.
func callTool(ctx context.Context, c *client.Client, toolName string, args map[string]interface{}) (string, error) {
	request := mcp.CallToolRequest{
		Params: mcp.CallToolParams{
			Name:      toolName,
			Arguments: args,
		},
	}
	result, err := c.CallTool(ctx, request)
	if err != nil {
		return "", err
	}
	if result == nil || result.IsError {
		return "", fmt.Errorf("tool call returned error: %v", result)
	}
	var text string
	for _, content := range result.Content {
		if tc, ok := content.(mcp.TextContent); ok {
			text += tc.Text
		}
	}
	return text, nil
}

// --- Source Implementations ---

// LocalMCPSource connects to tw-quant-mcp container via MCP HTTP protocol.
type LocalMCPSource struct {
	addr string
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

	text, err := callTool(ctx, c, "get_daily_prices", map[string]interface{}{
		"symbol":     symbol,
		"start_date": start.Format("2006-01-02"),
		"end_date":   end.Format("2006-01-02"),
	})
	if err != nil {
		return nil, err
	}
	if text == "" {
		return nil, nil
	}

	var prices []PriceData
	if err := json.Unmarshal([]byte(text), &prices); err != nil {
		return nil, fmt.Errorf("parse local-mcp response: %w", err)
	}
	return prices, nil
}

// TWSEMCPSource connects to TWSEMCPServer remote HTTP MCP endpoint.
type TWSEMCPSource struct {
	addr string
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
			addr = os.Getenv("TWSE_MCP_URL")
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

	text, err := callTool(ctx, c, "get_daily_prices", map[string]interface{}{
		"symbol":     symbol,
		"start_date": start.Format("2006-01-02"),
		"end_date":   end.Format("2006-01-02"),
	})
	if err != nil {
		return nil, err
	}
	if text == "" {
		return nil, nil
	}

	var prices []PriceData
	if err := json.Unmarshal([]byte(text), &prices); err != nil {
		return nil, fmt.Errorf("parse twse-mcp response: %w", err)
	}
	return prices, nil
}

// FinMindMCPSource fetches from FinMind MCP (stdio transport).
type FinMindMCPSource struct {
	addr    string   // unused (stdio transport), kept for API compat
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
			return nil, fmt.Errorf("FINMIND_API_KEY not set")
		}
		s.wrapper = &MCPClientWrapper{
			cmd: "uvx",
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

	text, err := callTool(ctx, c, "get_daily_prices", map[string]interface{}{
		"symbol":     symbol,
		"start_date": start.Format("2006-01-02"),
		"end_date":   end.Format("2006-01-02"),
	})
	if err != nil {
		return nil, err
	}
	if text == "" {
		return nil, nil
	}

	var prices []PriceData
	if err := json.Unmarshal([]byte(text), &prices); err != nil {
		return nil, fmt.Errorf("parse finmind-mcp response: %w", err)
	}
	return prices, nil
}

// YFinanceMCPSource fetches from yfinance MCP (stdio transport).
type YFinanceMCPSource struct {
	addr    string   // unused (stdio transport), kept for API compat
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
			args: []string{"yfinance-mcp"},
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

	text, err := callTool(ctx, c, "get_daily_prices", map[string]interface{}{
		"symbol":     symbol,
		"start_date": start.Format("2006-01-02"),
		"end_date":   end.Format("2006-01-02"),
	})
	if err != nil {
		return nil, err
	}
	if text == "" {
		return nil, nil
	}

	var prices []PriceData
	if err := json.Unmarshal([]byte(text), &prices); err != nil {
		return nil, fmt.Errorf("parse yfinance-mcp response: %w", err)
	}
	return prices, nil
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
