package recall_test

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/recall"
)

// slowService answers /recall and /recall/probe, and holds each request for
// as long as `delay` says before it answers. It keeps every live ask it read,
// and the body of every probe.
type slowService struct {
	*httptest.Server

	mu     sync.Mutex
	delay  time.Duration
	asks   []recall.Ask
	probes []map[string]any
}

func newSlowService(t *testing.T) *slowService {
	t.Helper()
	found := &slowService{}
	found.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		found.mu.Lock()
		if r.URL.Path == "/recall/probe" {
			var probe map[string]any
			json.Unmarshal(body, &probe)
			found.probes = append(found.probes, probe)
		} else {
			var ask recall.Ask
			json.Unmarshal(body, &ask)
			found.asks = append(found.asks, ask)
		}
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

// probed sends one probe with a deadline of 50 ms.
func probed(client *recall.Client, seen []int64) error {
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	_, err := client.Probe(ctx, recall.Ask{SessionID: "s2", Harness: "claude-code"}, seen)
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

// A probe that runs out of time is not a live miss, and a probe does not take
// the live count with it, so the live count holds only live misses.
func TestAProbeLeavesTheLiveMissCountAlone(t *testing.T) {
	at := newSlowService(t)
	client := &recall.Client{Service: at.URL, HTTP: at.Client()}

	at.hold(time.Second)
	if err := probed(client, nil); err == nil {
		t.Fatal("a probe held past its deadline answered")
	}
	if err := asked(client); err == nil {
		t.Fatal("an ask held past its deadline answered")
	}
	at.hold(0)
	if err := probed(client, nil); err != nil {
		t.Fatal(err)
	}
	if err := asked(client); err != nil {
		t.Fatal(err)
	}

	if got := at.missed(); !slices.Equal(got, []int{0, 1}) {
		t.Errorf("got the live misses %v sent, want [0 1]", got)
	}
}

// A probe goes to a route of its own and carries no count, because the
// service's probe route refuses a field it does not name.
func TestAProbeSendsWhatItWasHandedAndNoCount(t *testing.T) {
	at := newSlowService(t)
	client := &recall.Client{Service: at.URL, HTTP: at.Client()}

	if err := probed(client, []int64{7, 9}); err != nil {
		t.Fatal(err)
	}

	if len(at.probes) != 1 {
		t.Fatalf("got %d probes, want 1", len(at.probes))
	}
	if _, found := at.probes[0]["missed"]; found {
		t.Errorf("the probe carried a count: %v", at.probes[0])
	}
	if seen := at.probes[0]["seen"]; !reflect.DeepEqual(seen, []any{7.0, 9.0}) {
		t.Errorf("the probe named %v as seen, want [7 9]", seen)
	}
}
