// Package recall asks the service what this person's earlier sessions said,
// and renders the answer as the block a turn reads.
package recall

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

// Ask is the question the bastion puts to the service. The prompt goes whole.
// The service matches statements against it and applies its own limit to its
// length.
type Ask struct {
	SessionID string `json:"session_id"`
	Harness   string `json:"harness"`
	ScopeKey  string `json:"scope_key"`
	Prompt    string `json:"prompt"`
	Limit     int    `json:"limit"`
}

// Statement is one thing an earlier session said, with where it came from.
type Statement struct {
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
}

// Statements returns what the service has to say for this ask. The context
// carries the deadline, so a service that is slow to connect and one that is
// slow to answer both end the same way: no memory this time.
func (c *Client) Statements(ctx context.Context, ask Ask) ([]Statement, error) {
	body, err := json.Marshal(ask)
	if err != nil {
		return nil, err
	}
	address := strings.TrimSuffix(c.Service, "/") + "/recall"
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
