package top

import (
	"strings"
	"testing"
	"time"
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

func TestAStatementIsCutToAboutTwoLines(t *testing.T) {
	cases := []struct {
		name      string
		statement string
		want      string
	}{
		{"a short statement is unchanged", "keep the socket warm", "keep the socket warm"},
		{"whitespace collapses", "keep  the\n socket\twarm", "keep the socket warm"},
		{"an empty statement stays empty", "   ", ""},
		{
			"a statement at the limit is unchanged",
			strings.Repeat("a", statementChars),
			strings.Repeat("a", statementChars),
		},
		{
			"a statement over the limit is cut and marked",
			strings.Repeat("a", statementChars+20),
			strings.Repeat("a", statementChars) + "…",
		},
		{
			"a cut in a space drops the space",
			strings.Repeat("a", statementChars-1) + " word",
			strings.Repeat("a", statementChars-1) + "…",
		},
	}
	for _, cut := range cases {
		t.Run(cut.name, func(t *testing.T) {
			if got := twoLines(cut.statement); got != cut.want {
				t.Errorf("twoLines(%q) = %q, want %q", cut.statement, got, cut.want)
			}
		})
	}
}

func TestAMomentIsDrawnAsHowLongAgoItWas(t *testing.T) {
	now := time.Date(2026, 3, 4, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		name string
		when string
		want string
	}{
		{"no moment draws nothing", "", ""},
		{"an unreadable moment draws nothing", "the other day", ""},
		{"seconds", "2026-03-04T11:59:31+00:00", "29s"},
		{"a minute is still minutes", "2026-03-04T11:59:00+00:00", "1m"},
		{"minutes", "2026-03-04T11:20:00+00:00", "40m"},
		{"hours", "2026-03-04T05:00:00+00:00", "7h"},
		{"days", "2026-02-25T12:00:00+00:00", "7d"},
		{"a moment with no zone is read as UTC", "2026-03-04T10:30:00.123456", "1h"},
		{"a moment with a zulu zone", "2026-03-04T11:00:00Z", "1h"},
		{"a moment in another zone", "2026-03-04T07:00:00-04:00", "1h"},
	}
	for _, moment := range cases {
		t.Run(moment.name, func(t *testing.T) {
			if got := ago(moment.when, now); got != moment.want {
				t.Errorf("ago(%q) = %q, want %q", moment.when, got, moment.want)
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

	got := frame(asked, drawn, size{rows: 24, columns: 80}, now)
	if got != want {
		t.Errorf("the frame was drawn as\n%s\nwant\n%s", visible(got), visible(want))
	}
}

// visible makes the escape sequences in a frame readable in a failure message.
func visible(frame string) string {
	return strings.ReplaceAll(frame, "\x1b", "\\x1b")
}

func TestAFrameThatCannotReachTheBastionSaysSo(t *testing.T) {
	drawn := state{problem: "cannot reach the bastion at /run/agentic-memory.sock: no such file"}
	got := frame(options{limit: 10, written: 10, read: 10}, drawn, size{rows: 24, columns: 80}, time.Now())

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
	got := frame(options{limit: 10, written: 10, read: 10}, drawn, size{rows: 5, columns: 80}, time.Now())
	if rows := strings.Count(got, "\n") + 1; rows != 5 {
		t.Errorf("the frame drew %d rows into a terminal of 5", rows)
	}
}
