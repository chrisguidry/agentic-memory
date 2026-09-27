package label

import (
	"fmt"
	"strings"
	"time"

	"github.com/chrisguidry/agentic-memory/host/terminal"
)

// legend is the one line of keys the person needs, shown on every frame so
// the TUI never has to be learned from a README.
const legend = "g good   n noise   w wrong   s skip   c context   q quit"

// header is where and when the prompt was said, so a session picked at
// random reads as a place and a moment rather than a statement in the void.
func header(pair Pair, now time.Time) terminal.Line {
	where := pair.SessionScopeKey
	if where == "" {
		where = "everywhere"
	}
	directory := pair.WorkingDirectory
	if directory == "" {
		directory = "(no working directory)"
	}
	return terminal.Line{{
		Text:  fmt.Sprintf("%s  ·  %s  ·  %s ago", where, directory, terminal.Ago(pair.OccurredAt, now)),
		Style: terminal.Dim,
	}}
}

// lastReplyPanel is what the agent told the person just before the prompt,
// so a prompt like "yeah do that" reads as an answer instead of nothing.
func lastReplyPanel(pair Pair, width int) terminal.Panel {
	body := wrapped(pair.LastReply, width)
	if len(body) == 0 {
		body = []terminal.Line{{{Text: "(nothing said before this, in this session)", Style: terminal.Dim}}}
	}
	return terminal.Panel{
		Title:  terminal.Line{{Text: "the agent's last reply", Style: terminal.Bold}},
		Border: terminal.White,
		Body:   body,
	}
}

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
		rows = append(rows, header(pair, now).Render())
		rows = append(rows, lastReplyPanel(pair, size.Columns).Draw(size.Columns)...)
		rows = append(rows, promptPanel(pair, size.Columns).Draw(size.Columns)...)
		rows = append(rows, statementPanel(pair, now, size.Columns).Draw(size.Columns)...)
		rows = append(rows, terminal.Line{
			{Text: status(sample, found.Done, found.Total, socket), Style: terminal.Dim},
		}.Render())
	}
	return strings.Join(terminal.Cut(rows, size.Rows), "\n")
}

// exchangePromptPanel is the human prompt of one earlier exchange.
func exchangePromptPanel(exchange Exchange, width int) terminal.Panel {
	body := wrapped(exchange.Prompt, width)
	if len(body) == 0 {
		body = []terminal.Line{{{Text: "(no prompt found for this exchange)", Style: terminal.Dim}}}
	}
	return terminal.Panel{
		Title:  terminal.Line{{Text: "an earlier prompt", Style: terminal.Bold}},
		Border: terminal.Cyan,
		Body:   body,
	}
}

// exchangeRepliesPanel is the agent's replies to one earlier exchange,
// joined the way the exchanges before the message are joined for the
// classifier, so more than one reply still reads as one panel.
func exchangeRepliesPanel(exchange Exchange, width int) terminal.Panel {
	body := wrapped(strings.Join(exchange.Replies, "\n\n"), width)
	if len(body) == 0 {
		body = []terminal.Line{{{Text: "(no reply)", Style: terminal.Dim}}}
	}
	return terminal.Panel{
		Title:  terminal.Line{{Text: "its replies", Style: terminal.Bold}},
		Border: terminal.White,
		Body:   body,
	}
}

// contextFrame is one redraw of the context view: one earlier exchange, or
// why there is none, with its own legend so paging back is learned from the
// screen rather than the README.
func contextFrame(exchange *Exchange, page int, problem string, size terminal.Size, socket string) string {
	var rows []string
	switch {
	case problem != "":
		rows = terminal.Panel{
			Title:  terminal.Line{{Text: "earlier context", Style: terminal.Bold}},
			Border: terminal.Red,
			Body:   []terminal.Line{{{Text: problem, Style: terminal.Red}}},
		}.Draw(size.Columns)
	case exchange == nil:
		rows = terminal.Panel{
			Title:  terminal.Line{{Text: "earlier context", Style: terminal.Bold}},
			Border: terminal.Yellow,
			Body:   []terminal.Line{{{Text: "nothing earlier in this session.", Style: terminal.Dim}}},
		}.Draw(size.Columns)
	default:
		rows = append(rows, exchangePromptPanel(*exchange, size.Columns).Draw(size.Columns)...)
		rows = append(rows, exchangeRepliesPanel(*exchange, size.Columns).Draw(size.Columns)...)
	}
	rows = append(rows, terminal.Line{
		{Text: fmt.Sprintf("  page %d  ·  c older   any other key back   ·   %s", page, socket), Style: terminal.Dim},
	}.Render())
	return strings.Join(terminal.Cut(rows, size.Rows), "\n")
}
