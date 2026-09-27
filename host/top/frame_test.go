package top

import (
	"strings"
	"testing"
	"time"

	"github.com/chrisguidry/agentic-memory/host/terminal"
)

func TestEveryPanelGivesWayUntilTheFrameFits(t *testing.T) {
	cases := []struct {
		name                             string
		height                           int
		limit, written, read             int
		wantLimit, wantWritten, wantRead int
	}{
		{"a tall terminal draws what was asked", 60, 10, 10, 10, 10, 10, 10},
		{"the ranked list gives way first", 50, 10, 10, 10, 7, 10, 10},
		{"a short terminal keeps the two feeds whole", 30, 10, 10, 10, 1, 10, 10},
		{"the written feed gives way after the list", 20, 10, 10, 10, 1, 1, 9},
		{"the readings feed gives way last", 17, 10, 10, 10, 1, 1, 6},
		{"every panel keeps one row", 7, 10, 10, 10, 1, 1, 1},
		{"a terminal with no room keeps one row each", 1, 10, 10, 10, 1, 1, 1},
		{"a count below the room is not raised", 60, 3, 2, 1, 3, 2, 1},
	}
	for _, sized := range cases {
		t.Run(sized.name, func(t *testing.T) {
			limit, written, read := fitting(sized.height, sized.limit, sized.written, sized.read)
			if limit != sized.wantLimit || written != sized.wantWritten || read != sized.wantRead {
				t.Errorf("fitting(%d, %d, %d, %d) = %d, %d, %d, want %d, %d, %d",
					sized.height, sized.limit, sized.written, sized.read,
					limit, written, read,
					sized.wantLimit, sized.wantWritten, sized.wantRead)
			}
		})
	}
}

// probability is what a reading gave one kind, as the JSON gives it.
func probability(answer float64) *float64 { return &answer }

func TestAFrameDrawsTheThreePanels(t *testing.T) {
	now := time.Date(2026, 3, 4, 12, 0, 0, 0, time.UTC)
	drawn := state{
		memories: []Memory{{
			Statement: "The build runs from the repository root.",
			Kind:      "procedural",
			ScopeKey:  "example.test/acme/widget",
			Rank:      0.87,
			CreatedAt: "2026-03-04T10:00:00+00:00",
			Actor:     "someone",
		}, {
			Statement: "Plain words win.",
			Kind:      "preference",
			ScopeKey:  "",
			Rank:      0.62,
			CreatedAt: "2026-03-03T12:00:00+00:00",
			Actor:     "",
		}},
		written: []Memory{{
			Statement: "Ship the socket first.",
			Kind:      "prospective",
			ScopeKey:  "example.test/acme/widget",
			Rank:      0.40,
			CreatedAt: "2026-03-04T11:59:30+00:00",
		}},
		readings: []Reading{{
			Message:      "let us roll",
			ClassifiedAt: "2026-03-04T11:59:00+00:00",
			Semantic:     probability(0.10),
			Praise:       probability(0.72),
		}},
	}
	asked := options{
		scope:   "example.test/acme/widget",
		limit:   2,
		written: 1,
		read:    1,
		socket:  "/run/agentic-memory.sock",
	}

	want := strings.Join([]string{
		"\x1b[32m╭\x1b[0m\x1b[32m────────────────────\x1b[0m \x1b[1mtop of mind\x1b[0m\x1b[2m example.test/acme/widget\x1b[0m \x1b[32m────────────────────\x1b[0m\x1b[32m╮\x1b[0m",
		"\x1b[32m│ \x1b[0m\x1b[2m 0.87 \x1b[0m\x1b[32mprocedural\x1b[0m\x1b[2m  example.test/acme/widget  2h  by someone\x1b[0m                  \x1b[32m │\x1b[0m",
		"\x1b[32m│ \x1b[0m      The build runs from the repository root.                              \x1b[32m │\x1b[0m",
		"\x1b[32m│ \x1b[0m\x1b[2m 0.62 \x1b[0m\x1b[35mpreference\x1b[0m\x1b[2m  everywhere  1d  by nobody recorded\x1b[0m                        \x1b[32m │\x1b[0m",
		"\x1b[32m│ \x1b[0m      Plain words win.                                                      \x1b[32m │\x1b[0m",
		"\x1b[32m╰\x1b[0m\x1b[32m────────────────────────────────\x1b[0m \x1b[2m2 statements\x1b[0m \x1b[32m────────────────────────────────\x1b[0m\x1b[32m╯\x1b[0m",
		"\x1b[35m╭\x1b[0m\x1b[35m────────────────────────────────\x1b[0m \x1b[1mjust written\x1b[0m \x1b[35m────────────────────────────────\x1b[0m\x1b[35m╮\x1b[0m",
		"\x1b[35m│ \x1b[0m\x1b[2m   30s \x1b[0m\x1b[33mprospective\x1b[0m\x1b[2m  0.40  example.test/acme/widget  \x1b[0mShip the socket first.  \x1b[35m │\x1b[0m",
		"\x1b[35m╰\x1b[0m\x1b[35m────────────────────────────────\x1b[0m \x1b[2m1 statement\x1b[0m \x1b[35m─────────────────────────────────\x1b[0m\x1b[35m╯\x1b[0m",
		"\x1b[34m╭\x1b[0m\x1b[34m─────────────────────────────────\x1b[0m \x1b[1mjust read\x1b[0m \x1b[34m──────────────────────────────────\x1b[0m\x1b[34m╮\x1b[0m",
		"\x1b[34m│ \x1b[0m\x1b[2m      1m \x1b[0m\x1b[34m0.72 praise\x1b[0m\x1b[2m  let us roll\x1b[0m                                           \x1b[34m │\x1b[0m",
		"\x1b[34m╰\x1b[0m\x1b[34m─────────────────────────────────\x1b[0m \x1b[2m1 reading\x1b[0m \x1b[34m──────────────────────────────────\x1b[0m\x1b[34m╯\x1b[0m",
		"\x1b[2m  2 top of mind · 1 just written · 1 just read · polling /run/agentic-memory.sock\x1b[0m",
	}, "\n")

	got := frame(asked, drawn, terminal.Size{Rows: 24, Columns: 80}, now)
	if got != want {
		t.Errorf("the frame was drawn as\n%s\nwant\n%s", visible(got), visible(want))
	}
}

// visible makes the escape sequences in a frame readable in a failure message.
func visible(frame string) string {
	return strings.ReplaceAll(frame, "\x1b", "\\x1b")
}

func TestTheStatusLineSaysHowManyTranscriptsAreBehind(t *testing.T) {
	cases := []struct {
		name   string
		behind int
		want   string
	}{
		{"none behind says nothing", 0, "1 just read · polling"},
		{"one behind", 1, "1 just read · 1 transcript behind · polling"},
		{"several behind", 3, "1 just read · 3 transcripts behind · polling"},
	}
	for _, drawn := range cases {
		t.Run(drawn.name, func(t *testing.T) {
			asked := options{limit: 1, written: 1, read: 1, socket: "/run/agentic-memory.sock"}
			got := frame(asked, state{behind: drawn.behind}, terminal.Size{Rows: 24, Columns: 80}, time.Now())
			if !strings.Contains(got, drawn.want) {
				t.Errorf("the status line does not say %q:\n%s", drawn.want, visible(got))
			}
		})
	}
}

func TestAFrameThatCannotReachTheBastionSaysSo(t *testing.T) {
	drawn := state{problem: "cannot reach the bastion at /run/agentic-memory.sock: no such file"}
	got := frame(options{limit: 10, written: 10, read: 10}, drawn, terminal.Size{Rows: 24, Columns: 80}, time.Now())

	if !strings.Contains(got, "cannot reach the bastion") {
		t.Errorf("the frame did not say the bastion is unreachable:\n%s", visible(got))
	}
	if strings.Contains(got, "polling") {
		t.Errorf("the frame drew a status line with no poll to report:\n%s", visible(got))
	}
	if rows := strings.Count(got, "\n") + 1; rows != 9 {
		t.Errorf("the unreachable frame drew %d rows, want 9", rows)
	}
}

func TestAFrameIsCutToTheRowsTheTerminalHas(t *testing.T) {
	drawn := state{}
	got := frame(options{limit: 10, written: 10, read: 10}, drawn, terminal.Size{Rows: 5, Columns: 80}, time.Now())
	if rows := strings.Count(got, "\n") + 1; rows != 5 {
		t.Errorf("the frame drew %d rows into a terminal of 5", rows)
	}
}
