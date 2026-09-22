package top

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Memory is one statement the service is holding, with the rank the order was
// computed from.
type Memory struct {
	Statement string  `json:"statement"`
	Kind      string  `json:"kind"`
	ScopeKey  string  `json:"scope_key"`
	Rank      float64 `json:"rank"`
	CreatedAt string  `json:"created_at"`
	Actor     string  `json:"actor"`
}

// Reading is one message the classifier read, with a probability for each kind
// it answered. A kind the reading has no answer for is absent, which is
// different from an answer of zero.
type Reading struct {
	Message      string   `json:"message"`
	ClassifiedAt string   `json:"classified_at"`
	Semantic     *float64 `json:"semantic"`
	Procedural   *float64 `json:"procedural"`
	Prospective  *float64 `json:"prospective"`
	Preference   *float64 `json:"preference"`
	Correction   *float64 `json:"correction"`
	Praise       *float64 `json:"praise"`
}

// probability returns what the reading gave one kind, and whether it gave one.
func (r Reading) probability(kind string) (float64, bool) {
	var answer *float64
	switch kind {
	case "semantic":
		answer = r.Semantic
	case "procedural":
		answer = r.Procedural
	case "prospective":
		answer = r.Prospective
	case "preference":
		answer = r.Preference
	case "correction":
		answer = r.Correction
	case "praise":
		answer = r.Praise
	}
	if answer == nil {
		return 0, false
	}
	return *answer, true
}

// best returns the kind the reading answered highest, and that answer.
func (r Reading) best() (string, float64) {
	winner, highest := "", 0.0
	for _, kind := range kinds() {
		answer, given := r.probability(kind)
		if !given {
			continue
		}
		if winner == "" || answer > highest {
			winner, highest = kind, answer
		}
	}
	return winner, highest
}

// timeout bounds one poll. The bastion answers from a warm connection, so a
// poll that takes this long means the service is gone rather than slow.
const timeout = 5 * time.Second

// Client reads the bastion's socket. The bastion adds the authorization header
// on the way to the service, so nothing here holds a credential.
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

// Memories returns what is worth remembering for a scope, in an order the
// caller names: `rank` for what a turn would be handed, `newest` for what the
// writer last produced.
func (c *Client) Memories(ctx context.Context, scope string, limit int, order string) ([]Memory, error) {
	query := url.Values{"limit": {strconv.Itoa(limit)}, "order": {order}}
	if scope != "" {
		query.Set("scope_key", scope)
	}
	var found []Memory
	return found, c.get(ctx, "/memories", query, &found)
}

// Classifications returns the messages the classifier read most recently,
// whatever it made of them.
func (c *Client) Classifications(ctx context.Context, limit int) ([]Reading, error) {
	query := url.Values{"above": {"0.0"}, "limit": {strconv.Itoa(limit)}}
	var found []Reading
	return found, c.get(ctx, "/classifications", query, &found)
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
