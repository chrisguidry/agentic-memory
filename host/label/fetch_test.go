package label

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"
)

// asks is what the client sent, held under a lock because the server answers
// on its own goroutine.
type asks struct {
	mutex sync.Mutex
	sent  []*http.Request
}

func (a *asks) record(request *http.Request) {
	a.mutex.Lock()
	defer a.mutex.Unlock()
	a.sent = append(a.sent, request)
}

func (a *asks) first() *http.Request {
	a.mutex.Lock()
	defer a.mutex.Unlock()
	if len(a.sent) == 0 {
		return nil
	}
	return a.sent[0]
}

// bastion answers on a unix socket the way the real one does, with no
// authorization header of its own, and records every request it took.
func bastion(t *testing.T, routes http.Handler) (*Client, *asks) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "agentic-memory.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatalf("could not listen on %s: %v", path, err)
	}
	asked := &asks{}
	server := httptest.NewUnstartedServer(http.HandlerFunc(
		func(writer http.ResponseWriter, request *http.Request) {
			asked.record(request)
			routes.ServeHTTP(writer, request)
		}))
	server.Listener.Close()
	server.Listener = listener
	server.Start()
	t.Cleanup(server.Close)
	return Dial(path), asked
}

func TestNextAsksForASampleAndAnAfter(t *testing.T) {
	client, asked := bastion(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		writer.Write([]byte(`{"done":1,"total":3,"pair":{"id":9,"session_id":"s1",` +
			`"entry_id":"e1","memory_id":42,"prompt":"how do we deploy?",` +
			`"statement":"we use uv here","kind":"procedural","scope_key":null,` +
			`"said_at":"2026-03-04T10:00:00+00:00"}}`))
	}))

	found, err := client.Next(context.Background(), "week-1", 5)
	if err != nil {
		t.Fatalf("Next: %v", err)
	}
	if found.Done != 1 || found.Total != 3 {
		t.Errorf("got done=%d total=%d, want 1, 3", found.Done, found.Total)
	}
	if found.Pair == nil || found.Pair.Statement != "we use uv here" {
		t.Errorf("the socket gave %+v", found.Pair)
	}
	want := "/labels/next?after=5&sample=week-1"
	if got := asked.first().URL.RequestURI(); got != want {
		t.Errorf("the client asked for %q, want %q", got, want)
	}
}

func TestNextWithNoAfterLeavesItOffTheQuery(t *testing.T) {
	client, asked := bastion(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Write([]byte(`{"done":0,"total":0,"pair":null}`))
	}))

	found, err := client.Next(context.Background(), "week-1", 0)
	if err != nil {
		t.Fatalf("Next: %v", err)
	}
	if found.Pair != nil {
		t.Errorf("a pair of null came back as %+v", found.Pair)
	}
	if got := asked.first().URL.RequestURI(); got != "/labels/next?sample=week-1" {
		t.Errorf("the client asked for %q", got)
	}
}

func TestJudgeSendsThePairAndTheLabel(t *testing.T) {
	client, asked := bastion(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Write([]byte(`{"labelled":"good"}`))
	}))

	pair := Pair{SessionID: "s1", EntryID: "e1", MemoryID: 42}
	if err := client.Judge(context.Background(), pair, "good"); err != nil {
		t.Fatalf("Judge: %v", err)
	}
	sent := asked.first()
	if sent.Method != http.MethodPost || sent.URL.Path != "/labels" {
		t.Errorf("Judge sent %s %s", sent.Method, sent.URL.Path)
	}
}

func TestASocketNobodyServesIsAnError(t *testing.T) {
	client := Dial(filepath.Join(t.TempDir(), "absent.sock"))
	if _, err := client.Next(context.Background(), "week-1", 0); err == nil {
		t.Error("a socket nobody serves answered without an error")
	}
}
