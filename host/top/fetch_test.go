package top

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"
)

// asks is what the clients sent, held under a lock because the server answers
// on its own goroutine.
type asks struct {
	mutex sync.Mutex
	sent  []string
}

func (a *asks) record(query string) {
	a.mutex.Lock()
	defer a.mutex.Unlock()
	a.sent = append(a.sent, query)
}

func (a *asks) first() string {
	a.mutex.Lock()
	defer a.mutex.Unlock()
	if len(a.sent) == 0 {
		return ""
	}
	return a.sent[0]
}

// bastion answers on a unix socket the way the real one does, with no
// authorization header of its own, and records the query each client sent.
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
			asked.record(request.URL.RequestURI())
			routes.ServeHTTP(writer, request)
		}))
	server.Listener.Close()
	server.Listener = listener
	server.Start()
	t.Cleanup(server.Close)
	return Dial(path), asked
}

func TestMemoriesComeBackFromTheSocket(t *testing.T) {
	client, asked := bastion(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		writer.Write([]byte(`[{"statement":"Plain words win.","kind":"preference",` +
			`"scope_key":null,"rank":0.62,"created_at":"2026-03-04T10:00:00+00:00",` +
			`"actor":"someone"}]`))
	}))

	found, err := client.Memories(context.Background(), "example.test/acme/widget", 5, "rank")
	if err != nil {
		t.Fatalf("Memories: %v", err)
	}
	if len(found) != 1 || found[0].Statement != "Plain words win." || found[0].Rank != 0.62 {
		t.Errorf("the socket gave %+v", found)
	}
	if found[0].ScopeKey != "" {
		t.Errorf("a statement scoped to nothing came back as %q", found[0].ScopeKey)
	}
	want := "/memories?limit=5&order=rank&scope_key=example.test%2Facme%2Fwidget"
	if asked.first() != want {
		t.Errorf("the client asked for %q, want %q", asked.first(), want)
	}
}

func TestEveryScopeLeavesTheScopeOffTheQuery(t *testing.T) {
	client, asked := bastion(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Write([]byte(`[]`))
	}))

	if _, err := client.Memories(context.Background(), "", 10, "newest"); err != nil {
		t.Fatalf("Memories: %v", err)
	}
	want := "/memories?limit=10&order=newest"
	if asked.first() != want {
		t.Errorf("the client asked for %q, want %q", asked.first(), want)
	}
}

func TestAReadingNamesTheKindItAnsweredHighest(t *testing.T) {
	client, asked := bastion(t, http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.Write([]byte(`[{"message":"let us roll","classified_at":"2026-03-04T10:00:00+00:00",` +
			`"semantic":0.1,"procedural":null,"praise":0.72}]`))
	}))

	found, err := client.Classifications(context.Background(), 3)
	if err != nil {
		t.Fatalf("Classifications: %v", err)
	}
	winner, highest := found[0].best()
	if winner != "praise" || highest != 0.72 {
		t.Errorf("the reading named %q at %v, want praise at 0.72", winner, highest)
	}
	want := "/classifications?above=0.0&limit=3"
	if asked.first() != want {
		t.Errorf("the client asked for %q, want %q", asked.first(), want)
	}
}

func TestAReadingWithNoAnswersNamesNothing(t *testing.T) {
	winner, highest := Reading{Message: "hello"}.best()
	if winner != "" || highest != 0 {
		t.Errorf("a reading with no answers named %q at %v", winner, highest)
	}
}

func TestASocketNobodyServesIsAnError(t *testing.T) {
	client := Dial(filepath.Join(t.TempDir(), "absent.sock"))
	if _, err := client.Memories(context.Background(), "", 10, "rank"); err == nil {
		t.Error("a socket nobody serves answered without an error")
	}
}
