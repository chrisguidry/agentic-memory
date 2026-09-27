package label

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Pair is one prompt and one statement it was handed, as the service answers
// it: enough to show a person and enough to send back with their judgment.
type Pair struct {
	ID        int    `json:"id"`
	SessionID string `json:"session_id"`
	EntryID   string `json:"entry_id"`
	MemoryID  int    `json:"memory_id"`
	Prompt    string `json:"prompt"`
	Statement string `json:"statement"`
	Kind      string `json:"kind"`
	ScopeKey  string `json:"scope_key"`
	SaidAt    string `json:"said_at"`

	// Where and when the prompt was said, and what the agent had just told
	// the person, so a prompt like "yeah do that" reads as an answer to
	// something instead of nothing on its own.
	OccurredAt       string `json:"occurred_at"`
	WorkingDirectory string `json:"working_directory"`
	SessionScopeKey  string `json:"session_scope_key"`
	LastReply        string `json:"last_reply"`
}

// Exchange is one earlier turn in the pair's session: a human prompt and the
// agent's replies to it, as `/labels/context` answers it.
type Exchange struct {
	EntryID    string   `json:"entry_id"`
	OccurredAt string   `json:"occurred_at"`
	Prompt     string   `json:"prompt"`
	Replies    []string `json:"replies"`
}

// Next is what `/labels/next` answers: the pair to show, and how far the
// sample has come. A nil pair means every pair the sample holds is judged.
type Next struct {
	Done  int   `json:"done"`
	Total int   `json:"total"`
	Pair  *Pair `json:"pair"`
}

// timeout bounds one call. The bastion answers from a warm connection, so a
// call that takes this long means the service is gone rather than slow.
const timeout = 5 * time.Second

// Client reads and writes through the bastion's socket. The bastion adds the
// authorization header on the way to the service, so nothing here holds a
// credential.
type Client struct {
	Socket string
	HTTP   *http.Client
}

// Dial returns a client for the socket at a path.
func Dial(path string) *Client {
	return &Client{
		Socket: path,
		HTTP: &http.Client{
			Timeout: timeout,
			Transport: &http.Transport{
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					return (&net.Dialer{}).DialContext(ctx, "unix", path)
				},
			},
		},
	}
}

// Next asks for the next unlabelled pair of a sample. `after` moves past a
// pair without judging it; zero starts from the beginning.
func (c *Client) Next(ctx context.Context, sample string, after int) (Next, error) {
	query := url.Values{"sample": {sample}}
	if after > 0 {
		query.Set("after", strconv.Itoa(after))
	}
	var found Next
	return found, c.get(ctx, "/labels/next", query, &found)
}

// contextAnswer is what `/labels/context` answers: a nil Exchange means
// paging has run past the start of the session.
type contextAnswer struct {
	Exchange *Exchange `json:"exchange"`
}

// Context asks for one earlier exchange in the pair's session: page 0 is the
// one right before the prompt at entryID, page 1 the one before that, and so
// on. A nil Exchange means there is nothing older left to show.
func (c *Client) Context(ctx context.Context, sessionID, entryID string, page int) (*Exchange, error) {
	query := url.Values{
		"session_id": {sessionID},
		"entry_id":   {entryID},
		"page":       {strconv.Itoa(page)},
	}
	var found contextAnswer
	if err := c.get(ctx, "/labels/context", query, &found); err != nil {
		return nil, err
	}
	return found.Exchange, nil
}

// Judge sends a person's judgment of one pair. A second judgment of the same
// pair replaces the first.
func (c *Client) Judge(ctx context.Context, pair Pair, judgment string) error {
	body, err := json.Marshal(map[string]any{
		"session_id": pair.SessionID,
		"entry_id":   pair.EntryID,
		"memory_id":  pair.MemoryID,
		"label":      judgment,
	})
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://bastion/labels", bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := c.HTTP.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("the bastion answered %s", response.Status)
	}
	return nil
}

func (c *Client) get(ctx context.Context, path string, query url.Values, into any) error {
	// The host is a name the unix dialer never reads, and every request goes
	// to the one socket the client was made with.
	address := "http://bastion" + path + "?" + query.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return err
	}
	response, err := c.HTTP.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("the bastion answered %s", response.Status)
	}
	return json.NewDecoder(response.Body).Decode(into)
}
