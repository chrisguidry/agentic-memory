package claudecode_test

import (
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/claudecode"
)

// listener is a bastion on a unix socket that keeps the path of every request
// and answers each with nothing. The hook reads the socket's path from the
// environment, which this sets.
type listener struct {
	mu    sync.Mutex
	paths []string
}

func listen(t *testing.T) *listener {
	t.Helper()
	found := &listener{}
	// The directory is short, because a socket's path has a length limit that
	// a test's own temporary directory can pass.
	directory, err := os.MkdirTemp("", "hook")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(directory) })
	path := filepath.Join(directory, "agentic-memory.sock")
	socket, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		found.mu.Lock()
		found.paths = append(found.paths, r.URL.Path)
		found.mu.Unlock()
	}))
	server.Listener.Close()
	server.Listener = socket
	server.Start()
	t.Cleanup(server.Close)
	t.Setenv("AGENTIC_MEMORY_SOCKET", path)
	return found
}

func (l *listener) posted() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return strings.Join(l.paths, " ")
}

// Only 1, true, 0, false, and empty pick a route. A typo forwards nothing,
// because a probe taken for a live session puts the probe's prompts in the
// store.
func TestTheProbeVariablePicksTheRoute(t *testing.T) {
	for _, test := range []struct {
		value  string
		paths  []string
		status int
	}{
		{"", []string{"/claude-code/hooks"}, 0},
		{"0", []string{"/claude-code/hooks"}, 0},
		{"false", []string{"/claude-code/hooks"}, 0},
		{"1", []string{"/claude-code/probes"}, 0},
		{"true", []string{"/claude-code/probes"}, 0},
		{"yes", nil, 1},
		{"TRUE", nil, 1},
		{"2", nil, 1},
	} {
		t.Run("AGENTIC_MEMORY_PROBE="+test.value, func(t *testing.T) {
			bastion := listen(t)
			t.Setenv("AGENTIC_MEMORY_PROBE", test.value)

			var stdout, stderr strings.Builder
			status := claudecode.Hook(strings.NewReader(`{"hook_event_name":"Stop"}`), &stdout, &stderr)

			if status != test.status {
				t.Errorf("the hook exited %d, want %d", status, test.status)
			}
			if posted := bastion.posted(); posted != strings.Join(test.paths, " ") {
				t.Errorf("the hook posted to %q, want %v", posted, test.paths)
			}
			if (stderr.Len() > 0) != (test.status != 0) {
				t.Errorf("the hook wrote %q to stderr", stderr.String())
			}
		})
	}
}
