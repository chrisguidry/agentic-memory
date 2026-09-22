package backfill_test

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/backfill"
)

// shipment is the body `POST /transcripts/ship` takes, declared here so the
// test reads the wire rather than the backfill's own type.
type shipment struct {
	Harness string `json:"harness"`
	Path    string `json:"path"`
	Machine string `json:"machine"`
	Cwd     string `json:"cwd"`
}

// bastion answers `POST /transcripts/ship` on a unix socket and keeps what it
// was asked for. Every file it is sent counts as two records, one of them new.
type bastion struct {
	*httptest.Server
	socket string

	mu        sync.Mutex
	shipments []shipment
}

func serve(t *testing.T) *bastion {
	t.Helper()
	found := &bastion{socket: filepath.Join(t.TempDir(), "bastion.sock")}
	listener, err := net.Listen("unix", found.socket)
	if err != nil {
		t.Fatal(err)
	}
	found.Server = &httptest.Server{
		Listener: listener,
		Config: &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/transcripts/ship" {
				http.NotFound(w, r)
				return
			}
			var asked shipment
			if err := json.NewDecoder(r.Body).Decode(&asked); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			found.mu.Lock()
			found.shipments = append(found.shipments, asked)
			found.mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"received":2,"inserted":1,"repeated":1}`))
		})},
	}
	found.Start()
	t.Cleanup(found.Close)
	return found
}

func TestABackfillShipsEveryFileAndReportsWhatTheServiceCounted(t *testing.T) {
	served := serve(t)
	source := t.TempDir()
	first := write(t, filepath.Join(source, "-work-widget", "11111111.jsonl"))
	second := write(t, filepath.Join(source, "-work-widget", "22222222.jsonl"))

	var out strings.Builder
	err := backfill.Run([]string{
		"--harness", "claude-code",
		"--from", source,
		"--machine", "spare",
		"--socket", served.socket,
	}, &out)
	if err != nil {
		t.Fatal(err)
	}

	want := []shipment{
		{Harness: "claude-code", Path: first, Machine: "spare"},
		{Harness: "claude-code", Path: second, Machine: "spare"},
	}
	if len(served.shipments) != len(want) || served.shipments[0] != want[0] || served.shipments[1] != want[1] {
		t.Errorf("the bastion was asked for %v, want %v", served.shipments, want)
	}

	printed := out.String()
	for _, line := range []string{
		"2 claude-code session files under " + source,
		"1/2 " + first + ": 1 new, 1 already held",
		"2/2 " + second + ": 1 new, 1 already held",
		"4 records, 2 new, 2 already held",
	} {
		if !strings.Contains(printed, line) {
			t.Errorf("the backfill printed no %q in:\n%s", line, printed)
		}
	}
}

func TestADryRunListsTheFilesAndShipsNothing(t *testing.T) {
	served := serve(t)
	source := t.TempDir()
	only := write(t, filepath.Join(source, "33333333.jsonl"))

	var out strings.Builder
	err := backfill.Run([]string{
		"--harness", "pi", "--from", source, "--socket", served.socket, "--dry-run",
	}, &out)
	if err != nil {
		t.Fatal(err)
	}

	if len(served.shipments) != 0 {
		t.Errorf("a dry run shipped %v", served.shipments)
	}
	if !strings.Contains(out.String(), only) {
		t.Errorf("a dry run listed no %s in:\n%s", only, out.String())
	}
}

func TestABastionThatIsNotThereIsAnError(t *testing.T) {
	source := t.TempDir()
	write(t, filepath.Join(source, "44444444.jsonl"))

	var out strings.Builder
	err := backfill.Run([]string{
		"--harness", "codex",
		"--from", source,
		"--socket", filepath.Join(t.TempDir(), "absent.sock"),
	}, &out)
	if err == nil {
		t.Fatal("a backfill with no bastion to ask returned no error")
	}
	if !strings.Contains(err.Error(), "could not reach the bastion") {
		t.Errorf("the error was %q", err)
	}
}

func TestAHarnessNobodyReads(t *testing.T) {
	var out strings.Builder
	err := backfill.Run([]string{"--harness", "emacs"}, &out)
	if err == nil {
		t.Fatal("an unknown harness returned no error")
	}
	if !strings.Contains(err.Error(), "claude-code, codex, pi") {
		t.Errorf("the error was %q", err)
	}
}
