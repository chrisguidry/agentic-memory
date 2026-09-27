// Package recall asks the service what this person's earlier sessions said,
// and renders the answer as the block a turn reads.
package recall

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptrace"
	"strings"
	"sync/atomic"
)

// Ask is the question the bastion puts to the service. The prompt goes whole.
// The service matches statements against it and applies its own limit to its
// length. Missed is how many asks since the last one sent ran out of time
// before the service answered, because the service has no other way to learn
// of them.
type Ask struct {
	SessionID string `json:"session_id"`
	Harness   string `json:"harness"`
	ScopeKey  string `json:"scope_key"`
	Prompt    string `json:"prompt"`
	Limit     int    `json:"limit"`
	Missed    int    `json:"missed"`
}

// probe is the body of `POST /recall/probe`: the ask without a count, and what
// the bastion handed the probe session before. The service records nothing
// for a probe, so Seen is the only record of what the session already has.
type probe struct {
	SessionID string  `json:"session_id"`
	Harness   string  `json:"harness"`
	ScopeKey  string  `json:"scope_key"`
	Prompt    string  `json:"prompt"`
	Limit     int     `json:"limit"`
	Seen      []int64 `json:"seen,omitempty"`
}

// Statement is one thing an earlier session said, with where it came from.
type Statement struct {
	ID         int64  `json:"id"`
	Statement  string `json:"statement"`
	Kind       string `json:"kind"`
	ScopeKey   string `json:"scope_key"`
	SaidAt     string `json:"said_at"`
	Actor      string `json:"actor"`
	ActorDepth int    `json:"actor_depth"`
}

// Client asks one service, over a connection it keeps warm between turns.
type Client struct {
	Service       string
	Authorization string
	HTTP          *http.Client

	missed atomic.Int64
}

// Statements returns what the service has to say for this ask. The context
// carries the deadline, so a service that is slow to connect and one that is
// slow to answer both end the same way: no memory this time.
//
// An ask that runs out of time is counted, and the count goes out with the
// next ask. The count is taken as delivered once the request is written,
// because the service counts it on arrival and before it answers. A request
// that was never written gives its count back for the ask after it.
func (c *Client) Statements(ctx context.Context, ask Ask) ([]Statement, error) {
	ask.Missed = int(c.missed.Swap(0))
	var wrote atomic.Bool
	ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{
		WroteRequest: func(info httptrace.WroteRequestInfo) { wrote.Store(info.Err == nil) },
	})
	statements, err := c.ask(ctx, "/recall", ask)
	if !wrote.Load() {
		c.missed.Add(int64(ask.Missed))
	}
	if errors.Is(err, context.DeadlineExceeded) {
		c.missed.Add(1)
	}
	return statements, err
}

// Probe returns what a live ask would be handed, from the service's probe
// route, which records nothing. Seen is what the bastion handed this probe
// session before.
//
// A probe that runs out of time is not counted, and a probe carries no count,
// so the count the next live ask sends holds only live misses. A service that
// has no probe route answers 404, and the probe gets no memory.
func (c *Client) Probe(ctx context.Context, ask Ask, seen []int64) ([]Statement, error) {
	return c.ask(ctx, "/recall/probe", probe{
		SessionID: ask.SessionID,
		Harness:   ask.Harness,
		ScopeKey:  ask.ScopeKey,
		Prompt:    ask.Prompt,
		Limit:     ask.Limit,
		Seen:      seen,
	})
}

func (c *Client) ask(ctx context.Context, route string, question any) ([]Statement, error) {
	body, err := json.Marshal(question)
	if err != nil {
		return nil, err
	}
	address := strings.TrimSuffix(c.Service, "/") + route
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, address, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	if c.Authorization != "" {
		request.Header.Set("Authorization", c.Authorization)
	}

	response, err := c.HTTP.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("the service answered %s", response.Status)
	}
	var answer struct {
		Statements []Statement `json:"statements"`
	}
	if err := json.NewDecoder(response.Body).Decode(&answer); err != nil {
		return nil, err
	}
	return answer.Statements, nil
}
