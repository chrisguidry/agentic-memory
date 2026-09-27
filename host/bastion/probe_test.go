package bastion_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/backfill"
	"github.com/chrisguidry/agentic-memory/host/bastion"
	"github.com/chrisguidry/agentic-memory/host/claudecode"
	"github.com/chrisguidry/agentic-memory/host/transcripts"
)

// upstream answers the routes a turn reaches, and keeps the route of every
// recall and the path of every transcript shipped.
type upstream struct {
	*httptest.Server

	mu      sync.Mutex
	asked   []string
	shipped []string
}

func newUpstream(t *testing.T) *upstream {
	t.Helper()
	found := &upstream{}
	recall := func(w http.ResponseWriter, r *http.Request) {
		found.mu.Lock()
		found.asked = append(found.asked, r.URL.Path)
		found.mu.Unlock()
		fmt.Fprint(w, `{"statements": [{"id": 7, "statement": "Tests come before code here.",
			"kind": "preference", "scope_key": "example.test/acme/widget", "actor": "human"}]}`)
	}
	routes := http.NewServeMux()
	routes.HandleFunc("POST /recall", recall)
	routes.HandleFunc("POST /recall/probe", recall)
	routes.HandleFunc("POST /v1/transcripts", func(w http.ResponseWriter, r *http.Request) {
		var sent struct {
			Path string `json:"path"`
		}
		json.NewDecoder(r.Body).Decode(&sent)
		found.mu.Lock()
		found.shipped = append(found.shipped, sent.Path)
		found.mu.Unlock()
		fmt.Fprint(w, `{"inserted": 1, "repeated": 0}`)
	})
	found.Server = httptest.NewServer(routes)
	t.Cleanup(found.Close)
	return found
}

func (u *upstream) recalls() []string {
	u.mu.Lock()
	defer u.mu.Unlock()
	return slices.Clone(u.asked)
}

func (u *upstream) shipments() []string {
	u.mu.Lock()
	defer u.mu.Unlock()
	return slices.Clone(u.shipped)
}

// onSocket serves the bastion's routes on a unix socket and points the hook at
// it. The directory is short, because a socket's path has a length limit that
// a test's own temporary directory can pass.
func onSocket(t *testing.T, at *upstream) (*httptest.Server, *transcripts.Shipper) {
	t.Helper()
	routes, shipper, err := bastion.Routes(bastion.Config{
		Service: at.URL, StateDir: t.TempDir(), Machine: "laptop", Limit: 10, Deadline: 5 * time.Second,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	directory, err := os.MkdirTemp("", "bastion")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(directory) })
	path := filepath.Join(directory, "agentic-memory.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(routes)
	server.Listener.Close()
	server.Listener = listener
	server.Start()
	t.Cleanup(server.Close)
	t.Setenv("AGENTIC_MEMORY_SOCKET", path)
	return server, shipper
}

// session writes an invented transcript of one entry at path.
func session(t *testing.T, path string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	line := `{"uuid":"entry-1","cwd":"/work/widget","text":"the first thing said"}` + "\n"
	if err := os.WriteFile(path, []byte(line), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// hook runs `agentic-memory claude` for one event, with AGENTIC_MEMORY_PROBE
// set to probe, and returns what it printed.
func hook(t *testing.T, probe, event, path string) string {
	t.Helper()
	t.Setenv("AGENTIC_MEMORY_PROBE", probe)
	payload, _ := json.Marshal(map[string]any{
		"session_id": "11111111-2222-3333-4444-555555555555", "hook_event_name": event,
		"cwd": "/work/widget", "prompt": "how do we ship this?", "transcript_path": path,
	})
	var printed strings.Builder
	claudecode.Hook(strings.NewReader(string(payload)), &printed, io.Discard)
	return printed.String()
}

// preflight asks the bastion's preflight what it records for a transcript.
func preflight(t *testing.T, socket *httptest.Server, path string) string {
	t.Helper()
	client := &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var dialer net.Dialer
			return dialer.DialContext(ctx, "unix", socket.Listener.Addr().String())
		},
	}, Timeout: 5 * time.Second}
	response, err := client.Get("http://bastion/claude-code/probes?transcript=" + url.QueryEscape(path))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var found struct {
		Probes bool   `json:"probes"`
		Record string `json:"record"`
	}
	if err := json.NewDecoder(response.Body).Decode(&found); err != nil {
		t.Fatal(err)
	}
	if !found.Probes {
		t.Errorf("the bastion says it takes no probes")
	}
	return found.Record
}

func TestTheHookMarksAProbeAndTheBastionShipsNothingForIt(t *testing.T) {
	for _, test := range []struct {
		variable string
		asked    string
		shipped  int
		record   string
	}{
		{"", "/recall", 1, "live"},
		{"0", "/recall", 1, "live"},
		{"1", "/recall/probe", 0, "probe"},
	} {
		t.Run("AGENTIC_MEMORY_PROBE="+test.variable, func(t *testing.T) {
			at := newUpstream(t)
			socket, shipper := onSocket(t, at)
			path := session(t, filepath.Join(t.TempDir(), "session.jsonl"))

			printed := hook(t, test.variable, "UserPromptSubmit", path)
			// Close waits for the handler, which ships after it answers.
			socket.Close()

			if !strings.Contains(printed, "Tests come before code here.") {
				t.Errorf("the hook printed %q", printed)
			}
			if asked := at.recalls(); !slices.Equal(asked, []string{test.asked}) {
				t.Errorf("got the asks %v, want one to %s", asked, test.asked)
			}
			if shipped := at.shipments(); len(shipped) != test.shipped {
				t.Errorf("got %d shipments, want %d", len(shipped), test.shipped)
			}
			if found := shipper.Recorded(path); string(found) != test.record {
				t.Errorf("the record is %q, want %q", found, test.record)
			}
		})
	}
}

// The bastion answers the preflight itself, and a probe's first event marks
// its transcript before the hook returns.
func TestTheBastionAnswersThePreflightForAProbe(t *testing.T) {
	at := newUpstream(t)
	socket, _ := onSocket(t, at)
	path := session(t, filepath.Join(t.TempDir(), "session.jsonl"))

	hook(t, "1", "UserPromptSubmit", path)

	if found := preflight(t, socket, path); found != "probe" {
		t.Errorf("the preflight says the record is %q, want probe", found)
	}
}

// A live event on a probe's transcript is what `claude --continue` and
// `claude --resume` send when the variable is not set.
func TestALiveEventAfterAProbeShipsNothingOfIt(t *testing.T) {
	at := newUpstream(t)
	socket, _ := onSocket(t, at)
	path := session(t, filepath.Join(t.TempDir(), "session.jsonl"))

	hook(t, "1", "UserPromptSubmit", path)
	hook(t, "1", "Stop", path)
	hook(t, "1", "SessionEnd", path)
	hook(t, "", "UserPromptSubmit", path)
	hook(t, "", "Stop", path)
	socket.Close()

	if shipped := at.shipments(); len(shipped) != 0 {
		t.Errorf("shipped %v, want nothing", shipped)
	}
}

// A backfill ships through the same shipper as the hook, so it ships nothing
// of a probe session: not its transcript, and not a subagent's under it.
func TestABackfillShipsNothingOfAProbe(t *testing.T) {
	at := newUpstream(t)
	socket, _ := onSocket(t, at)
	source := t.TempDir()
	probed := session(t, filepath.Join(source, "-work-widget", "11111111.jsonl"))
	session(t, filepath.Join(source, "-work-widget", "11111111", "subagents", "agent-1.jsonl"))
	live := session(t, filepath.Join(source, "-work-widget", "22222222.jsonl"))
	hook(t, "1", "UserPromptSubmit", probed)

	var out strings.Builder
	err := backfill.Run([]string{
		"--harness", "claude-code", "--from", source, "--socket", socket.Listener.Addr().String(),
	}, &out)
	if err != nil {
		t.Fatal(err)
	}

	if shipped := at.shipments(); !slices.Equal(shipped, []string{live}) {
		t.Errorf("shipped %v, want only %s", shipped, live)
	}
}
