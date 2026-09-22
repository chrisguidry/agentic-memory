package claudecode_test

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/claudecode"
	"github.com/chrisguidry/agentic-memory/host/recall"
	"github.com/chrisguidry/agentic-memory/host/transcripts"
)

const session = "11111111-2222-3333-4444-555555555555"

var now = time.Date(2026, 9, 21, 12, 0, 0, 0, time.UTC)

// service answers the two routes the bastion asks of it, and keeps what it was
// sent. It refuses transcripts while `refusing` is set.
type service struct {
	*httptest.Server
	statements []recall.Statement

	mu        sync.Mutex
	shipments int
	asks      []recall.Ask
	refusing  bool
}

func newService(t *testing.T, statements ...recall.Statement) *service {
	t.Helper()
	found := &service{statements: statements}
	routes := http.NewServeMux()
	routes.HandleFunc("POST /recall", func(w http.ResponseWriter, r *http.Request) {
		var ask recall.Ask
		json.NewDecoder(r.Body).Decode(&ask)
		found.mu.Lock()
		found.asks = append(found.asks, ask)
		found.mu.Unlock()
		json.NewEncoder(w).Encode(map[string]any{"statements": found.statements})
	})
	routes.HandleFunc("POST /v1/transcripts", func(w http.ResponseWriter, r *http.Request) {
		found.mu.Lock()
		defer found.mu.Unlock()
		if found.refusing {
			http.Error(w, "away", http.StatusServiceUnavailable)
			return
		}
		found.shipments++
		fmt.Fprint(w, `{"inserted": 1, "repeated": 0}`)
	})
	found.Server = httptest.NewServer(routes)
	t.Cleanup(found.Close)
	return found
}

func (s *service) refuse(refusing bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.refusing = refusing
}

func (s *service) shipped() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.shipments
}

// transcript writes an invented session of three entries.
func transcript(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "session.jsonl")
	lines := []string{
		`{"uuid":"entry-1","cwd":"/work/widget","version":"9.9.9","text":"the first thing said"}`,
		`{"uuid":"entry-2","text":"the answer"}`,
		`{"uuid":"entry-3","text":"the next thing said"}`,
	}
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// newHooks builds the route and puts it behind a server, so a test speaks to
// it the way `agentic-memory claude` does.
func newHooks(t *testing.T, at *service, open transcripts.Opener) (*httptest.Server, *transcripts.Shipper) {
	t.Helper()
	shipper := &transcripts.Shipper{
		Service:  at.URL,
		StateDir: t.TempDir(),
		HTTP:     at.Client(),
		Open:     open,
		Scope:    func(string) (string, string) { return "example.test/acme/widget", "repo" },
	}
	hooks := &claudecode.Hooks{
		Recall:   &recall.Client{Service: at.URL, HTTP: at.Client()},
		Shipper:  shipper,
		Scope:    func(string) (string, string) { return "example.test/acme/widget", "repo" },
		Machine:  "laptop",
		Limit:    10,
		Deadline: 2 * time.Second,
		Now:      func() time.Time { return now },
	}
	bastion := httptest.NewServer(hooks)
	t.Cleanup(bastion.Close)
	return bastion, shipper
}

// fire sends one hook payload and returns what the turn would print.
func fire(t *testing.T, bastion *httptest.Server, event map[string]any) string {
	t.Helper()
	body, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Timeout: 5 * time.Second}
	response, err := client.Post(bastion.URL+"/claude-code/hooks", "application/json", strings.NewReader(string(body)))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("got %s, want 200", response.Status)
	}
	answer, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return string(answer)
}

func TestTheResponseIsCompleteBeforeTheTranscriptIsOpened(t *testing.T) {
	at := newService(t)
	opened := make(chan struct{})
	released := make(chan struct{})
	// The transcript cannot be opened until the test lets it, so a bastion
	// that read the file before answering would never answer, and the read of
	// the response below would time out.
	open := func(path string) (io.ReadSeekCloser, error) {
		close(opened)
		<-released
		return os.Open(path)
	}
	bastion, _ := newHooks(t, at, open)
	path := transcript(t)

	done := make(chan string, 1)
	go func() {
		done <- fire(t, bastion, map[string]any{
			"session_id": session, "hook_event_name": "Stop",
			"cwd": "/work/widget", "transcript_path": path,
		})
	}()

	select {
	case answer := <-done:
		if answer != "" {
			t.Errorf("got %q on Stop, want nothing", answer)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the response never arrived, so the transcript was opened first")
	}

	select {
	case <-opened:
	case <-time.After(5 * time.Second):
		t.Fatal("the transcript was never opened")
	}
	close(released)
}

func TestAPromptGetsTheBlockInTheEnvelopeTheHookDocsSpecify(t *testing.T) {
	at := newService(t, recall.Statement{
		Statement: "Tests come before code here.",
		Kind:      "preference", ScopeKey: "example.test/acme/widget",
		SaidAt: now.AddDate(0, 0, -3).Format(time.RFC3339), Actor: "human",
	})
	bastion, _ := newHooks(t, at, nil)

	answer := fire(t, bastion, map[string]any{
		"session_id": session, "hook_event_name": "UserPromptSubmit",
		"cwd": "/work/widget", "prompt": "how do we ship this?",
		"transcript_path": transcript(t),
	})

	var found struct {
		Specific struct {
			Event   string `json:"hookEventName"`
			Context string `json:"additionalContext"`
		} `json:"hookSpecificOutput"`
	}
	if err := json.Unmarshal([]byte(answer), &found); err != nil {
		t.Fatalf("could not read %q: %v", answer, err)
	}
	if found.Specific.Event != "UserPromptSubmit" {
		t.Errorf("got the event %q", found.Specific.Event)
	}
	want := recall.Heading + "\n- preference, example.test/acme/widget, person, 3 days ago: Tests come before code here."
	if found.Specific.Context != want {
		t.Errorf("got\n%q\nwant\n%q", found.Specific.Context, want)
	}
	if len(at.asks) != 1 {
		t.Fatalf("got %d asks, want 1", len(at.asks))
	}
	if at.asks[0] != (recall.Ask{
		SessionID: session, Harness: "claude-code",
		ScopeKey: "example.test/acme/widget", Prompt: "how do we ship this?", Limit: 10,
	}) {
		t.Errorf("got the ask %+v", at.asks[0])
	}
}

// No memory is an empty stdout, and never an envelope with an empty block,
// because Claude Code adds whatever the hook prints to the conversation.
func TestNoMemoryIsAnEmptyAnswer(t *testing.T) {
	at := newService(t)
	bastion, _ := newHooks(t, at, nil)

	answer := fire(t, bastion, map[string]any{
		"session_id": session, "hook_event_name": "UserPromptSubmit",
		"cwd": "/work/widget", "prompt": "hi", "transcript_path": transcript(t),
	})
	if answer != "" {
		t.Errorf("got %q, want nothing", answer)
	}
}

func TestARefusedFileIsAcceptedAndStoredWhenTheServiceReturns(t *testing.T) {
	at := newService(t)
	bastion, shipper := newHooks(t, at, nil)
	path := transcript(t)
	stop := map[string]any{
		"session_id": session, "hook_event_name": "Stop",
		"cwd": "/work/widget", "transcript_path": path,
	}

	at.refuse(true)
	if answer := fire(t, bastion, stop); answer != "" {
		t.Errorf("got %q from a refused shipment, want nothing", answer)
	}
	// The shipment runs after the response, so the test waits for the refusal
	// to land rather than assuming it already has.
	behind(t, shipper, 1)
	if shipped := at.shipped(); shipped != 0 {
		t.Fatalf("got %d shipments stored, want 0", shipped)
	}

	at.refuse(false)
	shipper.RetryDue(t.Context(), time.Now().Add(time.Hour))
	behind(t, shipper, 0)
	if shipped := at.shipped(); shipped != 1 {
		t.Fatalf("got %d shipments stored, want 1", shipped)
	}

	// The offset advanced once, so the same file has nothing new to send.
	if _, err := shipper.Ship(t.Context(), transcripts.Request{
		Harness: "claude-code", Path: path, Machine: "laptop", Cwd: "/work/widget",
	}); err != nil {
		t.Fatal(err)
	}
	if shipped := at.shipped(); shipped != 1 {
		t.Errorf("got %d shipments stored, want 1", shipped)
	}
}

// behind waits for the shipper to hold the given number of refused files.
func behind(t *testing.T, shipper *transcripts.Shipper, want int) {
	t.Helper()
	for until := time.Now().Add(5 * time.Second); time.Now().Before(until); {
		if shipper.Behind() == want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("got %d files behind, want %d", shipper.Behind(), want)
}
