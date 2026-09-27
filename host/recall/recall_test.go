package recall_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/recall"
)

// slowService answers /recall, and holds each request for as long as `delay`
// says before it answers. It keeps every ask it read.
type slowService struct {
	*httptest.Server

	mu    sync.Mutex
	delay time.Duration
	asks  []recall.Ask
}

func newSlowService(t *testing.T) *slowService {
	t.Helper()
	found := &slowService{}
	found.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var ask recall.Ask
		json.NewDecoder(r.Body).Decode(&ask)
		found.mu.Lock()
		found.asks = append(found.asks, ask)
		delay := found.delay
		found.mu.Unlock()
		select {
		case <-time.After(delay):
		case <-r.Context().Done():
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"statements": []recall.Statement{}})
	}))
	t.Cleanup(found.Close)
	return found
}

func (s *slowService) hold(delay time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.delay = delay
}

func (s *slowService) missed() []int {
	s.mu.Lock()
	defer s.mu.Unlock()
	found := []int{}
	for _, ask := range s.asks {
		found = append(found, ask.Missed)
	}
	return found
}

// asked sends one ask with a deadline of 50 ms.
func asked(client *recall.Client) error {
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	_, err := client.Statements(ctx, recall.Ask{SessionID: "s1", Harness: "claude-code"})
	return err
}

func TestAMissIsSentWithTheNextAskAndThenCleared(t *testing.T) {
	at := newSlowService(t)
	client := &recall.Client{Service: at.URL, HTTP: at.Client()}

	at.hold(time.Second)
	if err := asked(client); err == nil {
		t.Fatal("an ask held past its deadline answered")
	}
	at.hold(0)
	for range 2 {
		if err := asked(client); err != nil {
			t.Fatal(err)
		}
	}

	if got := at.missed(); !slices.Equal(got, []int{0, 1, 0}) {
		t.Errorf("got the misses %v sent, want [0 1 0]", got)
	}
}

// A count goes out once it is written, whatever happens to the answer after.
// A count that never reached the service is kept for the next ask.
func TestACountThatWasNeverSentIsKept(t *testing.T) {
	at := newSlowService(t)
	client := &recall.Client{Service: at.URL, HTTP: at.Client()}

	at.hold(time.Second)
	if err := asked(client); err == nil {
		t.Fatal("an ask held past its deadline answered")
	}
	gone := httptest.NewServer(http.NotFoundHandler())
	gone.Close()
	client.Service = gone.URL
	if err := asked(client); err == nil {
		t.Fatal("an ask to a closed service answered")
	}
	client.Service = at.URL
	at.hold(0)
	if err := asked(client); err != nil {
		t.Fatal(err)
	}

	if got := at.missed(); !slices.Equal(got, []int{0, 1}) {
		t.Errorf("got the misses %v sent, want [0 1]", got)
	}
}
