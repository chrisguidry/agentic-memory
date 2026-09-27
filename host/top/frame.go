package top

import (
	"fmt"
	"strings"
	"time"

	"github.com/chrisguidry/agentic-memory/host/terminal"
)

// panelChrome is what the three panels cost in rows besides the rows of
// statements: two borders each and one line for the status.
const panelChrome = 7

// topOfMindRow is what one ranked statement costs. It is a header line and a
// statement cut to two, and a long kind or scope can push it to three, so the
// fit is planned against three. Planning high costs a row of statements, and
// planning low costs a panel.
const topOfMindRow = 3

// fitting returns how many statements each panel draws, given the rows the
// terminal has.
//
// A panel that runs off the bottom of the terminal is the panel nobody reads,
// so every count from the command line is an upper bound. The list at the top
// gives way first and the two feeds at the bottom give way last, because the
// feeds are what a person is watching.
func fitting(height, limit, written, read int) (int, int, int) {
	room := height - panelChrome
	over := func() bool { return topOfMindRow*limit+written+read > room }
	for limit > 1 && over() {
		limit--
	}
	for written > 1 && over() {
		written--
	}
	for read > 1 && over() {
		read--
	}
	return limit, written, read
}

// memoriesPanel is what is worth remembering, ranked the way a turn would read
// it.
//
// One statement per row, with its kind, where it applies, and how long ago it
// was written on the line above it. The number on the left is the rank the
// order was computed from, which weighs the kind against the age, so it is not
// the classifier's confidence.
func memoriesPanel(rows []Memory, scope string, width int, now time.Time) terminal.Panel {
	title := terminal.Line{{Text: "top of mind", Style: terminal.Bold}}
	if scope == "" {
		title = append(title, terminal.Span{Text: " all scopes", Style: terminal.Dim})
	} else {
		title = append(title, terminal.Span{Text: " " + scope, Style: terminal.Dim})
	}

	body := []terminal.Line{{{Text: "nothing yet. say something worth remembering.", Style: terminal.Dim}}}
	if len(rows) > 0 {
		body = nil
		for _, row := range rows {
			where := row.ScopeKey
			if where == "" {
				where = "everywhere"
			}
			// A statement scoped above this one is reached by inheritance, and
			// saying so is the difference between a list of what is here and a
			// list of everything the turn was handed.
			arrow := ""
			if scope != "" && where != "everywhere" && where != scope {
				arrow = " ↑"
			}
			actor := row.Actor
			if actor == "" {
				actor = "nobody recorded"
			}
			body = append(body, terminal.Line{
				{Text: fmt.Sprintf("%5.2f ", row.Rank), Style: terminal.Dim},
				{Text: row.Kind, Style: terminal.KindStyle(row.Kind)},
				{Text: fmt.Sprintf("  %s%s  %s  by %s", where, arrow, terminal.Ago(row.CreatedAt, now), actor), Style: terminal.Dim},
			})
			for _, wrapped := range terminal.Wrap(terminal.TwoLines(row.Statement), width-10, 2) {
				body = append(body, terminal.Line{{Text: "      " + wrapped, Style: terminal.Plain}})
			}
		}
	}

	return terminal.Panel{
		Title: title, Subtitle: terminal.Plural(len(rows), "statement"), Border: terminal.Green, Body: body,
	}
}

// writtenPanel is the statements the writer has just produced, newest first.
//
// A statement here can rank below everything in the panel above it and never
// appear there, which is the point of this one: a plan or an approval is worth
// watching arrive even when it is not worth reading yet.
func writtenPanel(rows []Memory, now time.Time) terminal.Panel {
	body := []terminal.Line{{{Text: "nothing written yet.", Style: terminal.Dim}}}
	if len(rows) > 0 {
		body = nil
		for _, row := range rows {
			where := row.ScopeKey
			if where == "" {
				where = "everywhere"
			}
			body = append(body, terminal.Line{
				{Text: fmt.Sprintf("%6s ", terminal.Ago(row.CreatedAt, now)), Style: terminal.Dim},
				{Text: row.Kind, Style: terminal.KindStyle(row.Kind)},
				{Text: fmt.Sprintf("  %.2f  %s  ", row.Rank, where), Style: terminal.Dim},
				{Text: strings.Join(strings.Fields(row.Statement), " "), Style: terminal.Plain},
			})
		}
	}
	return terminal.Panel{
		Title:    terminal.Line{{Text: "just written", Style: terminal.Bold}},
		Subtitle: terminal.Plural(len(rows), "statement"),
		Border:   terminal.Magenta,
		Body:     body,
	}
}

// readingsPanel is what the classifier read most recently, newest first.
//
// A message that cleared no threshold is here and nowhere else, which is how a
// reading that produced no memory is still seen.
func readingsPanel(rows []Reading, now time.Time) terminal.Panel {
	body := []terminal.Line{{{Text: "nothing read yet.", Style: terminal.Dim}}}
	if len(rows) > 0 {
		body = nil
		for _, row := range rows {
			winner, highest := row.best()
			body = append(body, terminal.Line{
				{Text: fmt.Sprintf("%8s ", terminal.Ago(row.ClassifiedAt, now)), Style: terminal.Dim},
				{Text: fmt.Sprintf("%.2f %s", highest, winner), Style: terminal.KindStyle(winner)},
				{Text: "  " + strings.Join(strings.Fields(row.Message), " "), Style: terminal.Dim},
			})
		}
	}
	return terminal.Panel{
		Title:    terminal.Line{{Text: "just read", Style: terminal.Bold}},
		Subtitle: terminal.Plural(len(rows), "reading"),
		Border:   terminal.Blue,
		Body:     body,
	}
}

// state is what the last poll returned, and how it failed.
type state struct {
	memories []Memory
	written  []Memory
	readings []Reading
	behind   int
	problem  string
}

// status says what is drawn, what the terminal had no room for, and how many
// transcripts the bastion has not shipped. A count of zero is left off, so the
// line changes only when something is behind.
func status(asked options, drew [3]int, behind int) string {
	named := []struct {
		name         string
		asked, drawn int
	}{
		{"top of mind", asked.limit, drew[0]},
		{"just written", asked.written, drew[1]},
		{"just read", asked.read, drew[2]},
	}
	var parts []string
	for _, panel := range named {
		if panel.drawn == panel.asked {
			parts = append(parts, fmt.Sprintf("%d %s", panel.drawn, panel.name))
			continue
		}
		parts = append(parts, fmt.Sprintf("%d of %d %s", panel.drawn, panel.asked, panel.name))
	}
	switch {
	case behind == 1:
		parts = append(parts, "1 transcript behind")
	case behind > 1:
		parts = append(parts, fmt.Sprintf("%d transcripts behind", behind))
	}
	return "  " + strings.Join(parts, " · ") + " · polling " + asked.socket
}

// frame is one redraw, from whatever the last poll returned, fitted to the
// terminal. A frame longer than the terminal would scroll the one above it into
// view, so it is cut to the rows there are.
func frame(asked options, now state, size terminal.Size, clock time.Time) string {
	var rows []string
	if now.problem != "" {
		rows = append(rows, terminal.Panel{
			Title:  terminal.Line{{Text: "top of mind", Style: terminal.Bold}},
			Border: terminal.Red,
			Body:   []terminal.Line{{{Text: now.problem, Style: terminal.Red}}},
		}.Draw(size.Columns)...)
		rows = append(rows, terminal.Panel{
			Title:  terminal.Line{{Text: "just written", Style: terminal.Bold}},
			Border: terminal.White,
			Body:   []terminal.Line{{}},
		}.Draw(size.Columns)...)
		rows = append(rows, terminal.Panel{
			Title:  terminal.Line{{Text: "just read", Style: terminal.Bold}},
			Border: terminal.White,
			Body:   []terminal.Line{{}},
		}.Draw(size.Columns)...)
		return strings.Join(terminal.Cut(rows, size.Rows), "\n")
	}

	limit, written, read := fitting(size.Rows, asked.limit, asked.written, asked.read)
	rows = append(rows, memoriesPanel(first(now.memories, limit), asked.scope, size.Columns, clock).Draw(size.Columns)...)
	rows = append(rows, writtenPanel(first(now.written, written), clock).Draw(size.Columns)...)
	rows = append(rows, readingsPanel(first(now.readings, read), clock).Draw(size.Columns)...)
	rows = append(rows, terminal.Line{
		{Text: status(asked, [3]int{limit, written, read}, now.behind), Style: terminal.Dim},
	}.Render())
	return strings.Join(terminal.Cut(rows, size.Rows), "\n")
}

func first[T any](rows []T, count int) []T {
	if len(rows) > count {
		return rows[:count]
	}
	return rows
}
