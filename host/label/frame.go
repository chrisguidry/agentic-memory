package label

import (
	"fmt"
	"strings"
	"time"

	"github.com/chrisguidry/agentic-memory/host/terminal"
)

// legend is the one line of keys the person needs, shown on every frame so
// the TUI never has to be learned from a README.
const legend = "g good   n noise   w wrong   s skip   q quit"

// promptPanel is the prompt the pair was handed for.
func promptPanel(pair Pair, width int) terminal.Panel {
	body := wrapped(pair.Prompt, width)
	if len(body) == 0 {
		body = []terminal.Line{{{Text: "(no prompt found near this handout)", Style: terminal.Dim}}}
	}
	return terminal.Panel{
		Title:  terminal.Line{{Text: "the prompt", Style: terminal.Bold}},
		Border: terminal.Cyan,
		Body:   body,
	}
}

// statementPanel is the statement the turn was handed, with its kind, its
// scope, and how long ago it was said.
func statementPanel(pair Pair, now time.Time, width int) terminal.Panel {
	where := pair.ScopeKey
	if where == "" {
		where = "everywhere"
	}
	subtitle := terminal.Line{
		{Text: fmt.Sprintf("%s  ·  %s  ·  %s old", pair.Kind, where, terminal.Ago(pair.SaidAt, now)), Style: terminal.Dim},
	}
	body := wrapped(pair.Statement, width)
	return terminal.Panel{
		Title:    terminal.Line{{Text: "the statement", Style: terminal.Bold}},
		Subtitle: subtitle,
		Border:   terminal.KindStyle(pair.Kind),
		Body:     body,
	}
}

// wrapped is a block of text cut and wrapped to fit a panel of a width, at
// most six lines, so one long prompt or statement cannot push the legend off
// the bottom of the screen.
func wrapped(text string, width int) []terminal.Line {
	var body []terminal.Line
	for _, line := range terminal.Wrap(terminal.TwoLines(text), width-4, 6) {
		body = append(body, terminal.Line{{Text: line, Style: terminal.Plain}})
	}
	return body
}

// status is the progress through the sample, beside the legend.
func status(sample string, done, total int, socket string) string {
	return fmt.Sprintf("  %s: %d of %d labelled  ·  %s  ·  %s", sample, done, total, legend, socket)
}

// frame is one redraw: the pair to judge, or why there is none, fitted to the
// terminal. A frame longer than the terminal would scroll the panel above it
// out of view, so it is cut to the rows there are.
func frame(sample string, found Next, problem string, size terminal.Size, socket string, now time.Time) string {
	var rows []string
	switch {
	case problem != "":
		rows = terminal.Panel{
			Title:  terminal.Line{{Text: "the prompt", Style: terminal.Bold}},
			Border: terminal.Red,
			Body:   []terminal.Line{{{Text: problem, Style: terminal.Red}}},
		}.Draw(size.Columns)
	case found.Pair == nil:
		rows = terminal.Panel{
			Title:  terminal.Line{{Text: sample, Style: terminal.Bold}},
			Border: terminal.Green,
			Body: []terminal.Line{
				{{Text: fmt.Sprintf("every pair is labelled: %d of %d.", found.Done, found.Total), Style: terminal.Dim}},
			},
		}.Draw(size.Columns)
	default:
		pair := *found.Pair
		rows = append(rows, promptPanel(pair, size.Columns).Draw(size.Columns)...)
		rows = append(rows, statementPanel(pair, now, size.Columns).Draw(size.Columns)...)
		rows = append(rows, terminal.Line{
			{Text: status(sample, found.Done, found.Total, socket), Style: terminal.Dim},
		}.Render())
	}
	return strings.Join(terminal.Cut(rows, size.Rows), "\n")
}
