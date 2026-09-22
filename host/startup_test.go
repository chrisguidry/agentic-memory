package main

import (
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// budget is what a cold `agentic-memory claude` has. Claude Code runs it twice
// a turn, and the Python hooks it replaces cost 50 to 85 milliseconds each
// before they open a socket.
const budget = 5 * time.Millisecond

// runs is how many times the binary is timed. The best run is the measurement,
// because a machine that is busy adds time the binary did not spend.
const runs = 20

func TestAColdHookFinishesInsideItsBudget(t *testing.T) {
	binary := build(t)
	socket := filepath.Join(t.TempDir(), "absent.sock")
	payload := `{"session_id":"11111111-2222-3333-4444-555555555555",` +
		`"hook_event_name":"UserPromptSubmit","cwd":"/work/widget","prompt":"hi"}`

	best := time.Hour
	for range runs {
		run := exec.Command(binary, "claude")
		run.Env = append(run.Environ(), "AGENTIC_MEMORY_SOCKET="+socket)
		run.Stdin = strings.NewReader(payload)
		started := time.Now()
		out, err := run.CombinedOutput()
		taken := time.Since(started)
		if err != nil {
			t.Fatalf("the hook exited with %v: %s", err, out)
		}
		if len(out) != 0 {
			t.Fatalf("the hook printed %q with no bastion to ask", out)
		}
		best = min(best, taken)
	}

	t.Logf("the best of %d cold runs took %s", runs, best)
	if best > budget {
		t.Errorf("a cold hook took %s, over its budget of %s", best, budget)
	}
}

func build(t *testing.T) string {
	t.Helper()
	binary := filepath.Join(t.TempDir(), "agentic-memory")
	made := exec.Command("go", "build", "-o", binary, ".")
	if out, err := made.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v: %s", err, out)
	}
	return binary
}
