package label

import (
	"strings"
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/terminal"
)

func pair(prompt, statement, kind, scope string) *Pair {
	return &Pair{
		ID: 9, SessionID: "s1", EntryID: "e1", MemoryID: 42,
		Prompt: prompt, Statement: statement, Kind: kind, ScopeKey: scope,
		SaidAt: "2026-03-04T10:00:00+00:00",
	}
}

func TestAFrameShowsThePromptAndTheStatement(t *testing.T) {
	now := time.Date(2026, 3, 4, 12, 0, 0, 0, time.UTC)
	found := Next{Done: 3, Total: 10, Pair: pair("how do we deploy?", "we use uv here", "procedural", "example.test/acme/widget")}

	got := frame("week-1", found, "", terminal.Size{Rows: 24, Columns: 80}, "/run/agentic-memory.sock", now)

	for _, want := range []string{
		"how do we deploy?",
		"we use uv here",
		"procedural",
		"example.test/acme/widget",
		"2h",
		"3 of 10",
		"g good",
		"n noise",
		"w wrong",
		"s skip",
		"q quit",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("the frame did not show %q:\n%s", want, visible(got))
		}
	}
}

func TestAFrameWithNoScopeSaysEverywhere(t *testing.T) {
	found := Next{Done: 0, Total: 1, Pair: pair("hello", "a rule", "semantic", "")}
	got := frame("week-1", found, "", terminal.Size{Rows: 24, Columns: 80}, "sock", time.Now())
	if !strings.Contains(got, "everywhere") {
		t.Errorf("a pair with no scope did not say everywhere:\n%s", visible(got))
	}
}

func TestAFrameWithNoPromptSaysSo(t *testing.T) {
	found := Next{Done: 0, Total: 1, Pair: pair("", "a rule", "semantic", "")}
	got := frame("week-1", found, "", terminal.Size{Rows: 24, Columns: 80}, "sock", time.Now())
	if !strings.Contains(got, "no prompt found") {
		t.Errorf("a pair with no prompt did not say so:\n%s", visible(got))
	}
}

func TestAFrameWithNothingLeftSaysTheSampleIsDone(t *testing.T) {
	found := Next{Done: 5, Total: 5, Pair: nil}
	got := frame("week-1", found, "", terminal.Size{Rows: 24, Columns: 80}, "sock", time.Now())
	if !strings.Contains(got, "5 of 5") {
		t.Errorf("a finished sample did not say its counts:\n%s", visible(got))
	}
	if strings.Contains(got, "the prompt") {
		t.Errorf("a finished sample still drew a pair:\n%s", visible(got))
	}
}

func TestAFrameThatCannotReachTheBastionSaysSo(t *testing.T) {
	got := frame("week-1", Next{}, "cannot reach the bastion at sock: refused", terminal.Size{Rows: 24, Columns: 80}, "sock", time.Now())
	if !strings.Contains(got, "cannot reach the bastion") {
		t.Errorf("the frame did not say the bastion is unreachable:\n%s", visible(got))
	}
}

func TestAFrameIsCutToTheRowsTheTerminalHas(t *testing.T) {
	found := Next{Done: 0, Total: 1, Pair: pair("hello", "a rule", "semantic", "")}
	got := frame("week-1", found, "", terminal.Size{Rows: 5, Columns: 80}, "sock", time.Now())
	if rows := strings.Count(got, "\n") + 1; rows != 5 {
		t.Errorf("the frame drew %d rows into a terminal of 5, want 5", rows)
	}
}

func TestALongPromptStaysWithinTheTerminalHeight(t *testing.T) {
	long := strings.Repeat("word ", 400)
	found := Next{Done: 0, Total: 1, Pair: pair(long, long, "semantic", "")}
	got := frame("week-1", found, "", terminal.Size{Rows: 24, Columns: 80}, "sock", time.Now())
	if rows := strings.Count(got, "\n") + 1; rows > 24 {
		t.Errorf("a long pair drew %d rows into a terminal of 24", rows)
	}
}

// visible makes the escape sequences in a frame readable in a failure
// message.
func visible(frame string) string {
	return strings.ReplaceAll(frame, "\x1b", "\\x1b")
}
