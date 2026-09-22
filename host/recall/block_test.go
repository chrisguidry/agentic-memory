package recall_test

import (
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/recall"
)

var now = time.Date(2026, 9, 21, 12, 0, 0, 0, time.UTC)

func ago(days int) string {
	return now.AddDate(0, 0, -days).Format(time.RFC3339Nano)
}

// said builds one statement the way the service sends it.
func said(text string) recall.Statement {
	return recall.Statement{
		Statement:  text,
		Kind:       "preference",
		ScopeKey:   "example.test/acme/widget",
		SaidAt:     ago(3),
		Actor:      "human",
		ActorDepth: 0,
	}
}

func without(text string, change func(*recall.Statement)) recall.Statement {
	found := said(text)
	change(&found)
	return found
}

// expected is what `tools/recall.py` prints for these same statements. The Go
// block is checked against it byte for byte, so the two renderings cannot
// drift apart.
const expected = "Statements from this person's earlier sessions, chosen for this place and this prompt, with where each came from:\n" +
	"- preference, example.test/acme/widget, person, 3 days ago: Tests come before code here.\n" +
	"- correction, everywhere, agent at depth 1, 40 days ago: Commit messages say why.\n" +
	"- preference, example.test/acme/widget, person, today: Today it was said.\n" +
	"- preference, example.test/acme/widget, person, 1 day ago: Yesterday it was said.\n" +
	"- preference, example.test/acme/widget, person, undated: Nobody dated this.\n" +
	"- preference, example.test/acme/widget, unknown at depth 0, 3 days ago: An unknown actor said this.\n" +
	"- preference, example.test/acme/widget, human at depth 2, 3 days ago: A person at depth two."

func TestOneLinePerStatementWithItsProvenance(t *testing.T) {
	block := recall.Block([]recall.Statement{
		said("Tests come before code here."),
		without("Commit messages say why.", func(found *recall.Statement) {
			found.Kind, found.ScopeKey = "correction", ""
			found.Actor, found.ActorDepth, found.SaidAt = "agent", 1, ago(40)
		}),
		without("Today it was said.", func(found *recall.Statement) { found.SaidAt = ago(0) }),
		without("Yesterday it was said.", func(found *recall.Statement) { found.SaidAt = ago(1) }),
		without("Nobody dated this.", func(found *recall.Statement) { found.SaidAt = "" }),
		without("An unknown actor said this.", func(found *recall.Statement) { found.Actor = "" }),
		without("A person at depth two.", func(found *recall.Statement) { found.ActorDepth = 2 }),
	}, now)

	if block != expected {
		t.Errorf("got\n%s\nwant\n%s", block, expected)
	}
}

func TestATimestampWithNoZoneIsReadAsUTC(t *testing.T) {
	found := said("A naive timestamp.")
	found.SaidAt = "2026-09-18T12:00:00"
	block := recall.Block([]recall.Statement{found}, now)
	want := recall.Heading + "\n- preference, example.test/acme/widget, person, 3 days ago: A naive timestamp."
	if block != want {
		t.Errorf("got\n%s\nwant\n%s", block, want)
	}
}
