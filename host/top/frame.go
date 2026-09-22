package top

import (
	"fmt"
	"strings"
	"time"
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

// ago is how long ago a moment was, in as few characters as it takes.
func ago(when string, now time.Time) string {
	moment, ok := moment(when)
	if !ok {
		return ""
	}
	seconds := int(now.Sub(moment).Seconds())
	switch {
	case seconds < 60:
		return fmt.Sprintf("%ds", seconds)
	case seconds < 3600:
		return fmt.Sprintf("%dm", seconds/60)
	case seconds < 86400:
		return fmt.Sprintf("%dh", seconds/3600)
	}
	return fmt.Sprintf("%dd", seconds/86400)
}

// moment reads a timestamp the service wrote. A timestamp without a zone is
// read as UTC, which is the zone the service records in.
func moment(when string) (time.Time, bool) {
	if when == "" {
		return time.Time{}, false
	}
	for _, layout := range []string{
		time.RFC3339Nano,
		"2006-01-02T15:04:05.999999999",
		"2006-01-02T15:04:05",
	} {
		if read, err := time.Parse(layout, when); err == nil {
			return read.UTC(), true
		}
	}
	return time.Time{}, false
}

// panel is the box one list is drawn in.
type panel struct {
	title    line
	subtitle line
	border   string
	body     []line
}

// draw returns the panel's rows, with the title in the top border and the
// subtitle in the bottom, both centred.
func (p panel) draw(width int) []string {
	rows := []string{border("╭", "╮", p.title, p.border, width)}
	for _, body := range p.body {
		row := line{{"│ ", p.border}}
		row = append(row, body.truncate(width-4).pad(width-4)...)
		row = append(row, span{" │", p.border})
		rows = append(rows, row.render())
	}
	return append(rows, border("╰", "╯", p.subtitle, p.border, width))
}

func border(left, right string, label line, style string, width int) string {
	inner := width - 2
	if label.width() > 0 {
		label = append(line{{" ", plain}}, append(label.truncate(inner-4), span{" ", plain})...)
	}
	before := (inner - label.width()) / 2
	after := inner - label.width() - before
	row := line{{left, style}, {strings.Repeat("─", max(before, 0)), style}}
	row = append(row, label...)
	row = append(row, span{strings.Repeat("─", max(after, 0)), style}, span{right, style})
	return row.render()
}

// plural names a count of things the way a person writes it.
func plural(count int, thing string) line {
	word := thing
	if count != 1 {
		word += "s"
	}
	return line{{fmt.Sprintf("%d %s", count, word), dim}}
}

// memoriesPanel is what is worth remembering, ranked the way a turn would read
// it.
//
// One statement per row, with its kind, where it applies, and how long ago it
// was written on the line above it. The number on the left is the rank the
// order was computed from, which weighs the kind against the age, so it is not
// the classifier's confidence.
func memoriesPanel(rows []Memory, scope string, width int, now time.Time) panel {
	title := line{{"top of mind", bold}}
	if scope == "" {
		title = append(title, span{" all scopes", dim})
	} else {
		title = append(title, span{" " + scope, dim})
	}

	body := []line{{{"nothing yet. say something worth remembering.", dim}}}
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
			body = append(body, line{
				{fmt.Sprintf("%5.2f ", row.Rank), dim},
				{row.Kind, kindStyle(row.Kind)},
				{fmt.Sprintf("  %s%s  %s  by %s", where, arrow, ago(row.CreatedAt, now), actor), dim},
			})
			for _, wrapped := range wrap(twoLines(row.Statement), width-10, 2) {
				body = append(body, line{{"      " + wrapped, plain}})
			}
		}
	}

	return panel{title: title, subtitle: plural(len(rows), "statement"), border: green, body: body}
}

// writtenPanel is the statements the writer has just produced, newest first.
//
// A statement here can rank below everything in the panel above it and never
// appear there, which is the point of this one: a plan or an approval is worth
// watching arrive even when it is not worth reading yet.
func writtenPanel(rows []Memory, now time.Time) panel {
	body := []line{{{"nothing written yet.", dim}}}
	if len(rows) > 0 {
		body = nil
		for _, row := range rows {
			where := row.ScopeKey
			if where == "" {
				where = "everywhere"
			}
			body = append(body, line{
				{fmt.Sprintf("%6s ", ago(row.CreatedAt, now)), dim},
				{row.Kind, kindStyle(row.Kind)},
				{fmt.Sprintf("  %.2f  %s  ", row.Rank, where), dim},
				{strings.Join(strings.Fields(row.Statement), " "), plain},
			})
		}
	}
	return panel{
		title:    line{{"just written", bold}},
		subtitle: plural(len(rows), "statement"),
		border:   magenta,
		body:     body,
	}
}

// readingsPanel is what the classifier read most recently, newest first.
//
// A message that cleared no threshold is here and nowhere else, which is how a
// reading that produced no memory is still seen.
func readingsPanel(rows []Reading, now time.Time) panel {
	body := []line{{{"nothing read yet.", dim}}}
	if len(rows) > 0 {
		body = nil
		for _, row := range rows {
			winner, highest := row.best()
			body = append(body, line{
				{fmt.Sprintf("%8s ", ago(row.ClassifiedAt, now)), dim},
				{fmt.Sprintf("%.2f %s", highest, winner), kindStyle(winner)},
				{"  " + strings.Join(strings.Fields(row.Message), " "), dim},
			})
		}
	}
	return panel{
		title:    line{{"just read", bold}},
		subtitle: plural(len(rows), "reading"),
		border:   blue,
		body:     body,
	}
}

// state is what the last poll returned, and how it failed.
type state struct {
	memories []Memory
	written  []Memory
	readings []Reading
	problem  string
}

// status says what is drawn, and what the terminal had no room for.
func status(asked options, drew [3]int) string {
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
	return "  " + strings.Join(parts, " · ") + " · polling " + asked.socket
}

// frame is one redraw, from whatever the last poll returned, fitted to the
// terminal. A frame longer than the terminal would scroll the one above it into
// view, so it is cut to the rows there are.
func frame(asked options, now state, size size, clock time.Time) string {
	var rows []string
	if now.problem != "" {
		rows = append(rows, panel{
			title:  line{{"top of mind", bold}},
			border: red,
			body:   []line{{{now.problem, red}}},
		}.draw(size.columns)...)
		rows = append(rows, panel{
			title:  line{{"just written", bold}},
			border: white,
			body:   []line{{}},
		}.draw(size.columns)...)
		rows = append(rows, panel{
			title:  line{{"just read", bold}},
			border: white,
			body:   []line{{}},
		}.draw(size.columns)...)
		return strings.Join(cut(rows, size.rows), "\n")
	}

	limit, written, read := fitting(size.rows, asked.limit, asked.written, asked.read)
	rows = append(rows, memoriesPanel(first(now.memories, limit), asked.scope, size.columns, clock).draw(size.columns)...)
	rows = append(rows, writtenPanel(first(now.written, written), clock).draw(size.columns)...)
	rows = append(rows, readingsPanel(first(now.readings, read), clock).draw(size.columns)...)
	rows = append(rows, line{{status(asked, [3]int{limit, written, read}), dim}}.render())
	return strings.Join(cut(rows, size.rows), "\n")
}

func first[T any](rows []T, count int) []T {
	if len(rows) > count {
		return rows[:count]
	}
	return rows
}

func cut(rows []string, height int) []string {
	if height > 0 && len(rows) > height {
		return rows[:height]
	}
	return rows
}
