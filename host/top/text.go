package top

import (
	"strings"
	"unicode/utf8"
)

// Styles are written as ANSI select-graphic-rendition parameters rather than
// through a terminal library, because the binary is shared with the Claude Code
// hook and a library would add package initialization to every turn.
const (
	plain     = ""
	dim       = "2"
	bold      = "1"
	red       = "31"
	green     = "32"
	yellow    = "33"
	blue      = "34"
	magenta   = "35"
	cyan      = "36"
	white     = "37"
	homeClear = "\x1b[H\x1b[2J"
	hideCaret = "\x1b[?25l"
	showCaret = "\x1b[?25h"
)

// kindStyle is the colour each kind of statement is drawn in. It is a function
// rather than a map so that nothing in this package runs before main does.
func kindStyle(kind string) string {
	switch kind {
	case "semantic":
		return cyan
	case "procedural":
		return green
	case "prospective":
		return yellow
	case "preference":
		return magenta
	case "correction":
		return red
	case "praise":
		return blue
	}
	return white
}

// kinds is the order the classifier's readings are compared in. Two kinds with
// the same probability resolve to the one named first, the way the Python does.
func kinds() []string {
	return []string{"semantic", "procedural", "prospective", "preference", "correction", "praise"}
}

// span is a run of text in one style.
type span struct {
	text  string
	style string
}

// line is text with its styles, measured in characters rather than bytes, so a
// statement with accented letters still fits the column it is drawn in.
type line []span

func (l line) width() int {
	total := 0
	for _, s := range l {
		total += utf8.RuneCountInString(s.text)
	}
	return total
}

// truncate cuts a line to a width and marks the cut, so a long statement ends
// at the panel's edge instead of wrapping past it.
func (l line) truncate(width int) line {
	if l.width() <= width || width < 1 {
		return l
	}
	room := width - 1
	var cut line
	for _, s := range l {
		runes := []rune(s.text)
		if len(runes) <= room {
			cut = append(cut, s)
			room -= len(runes)
			continue
		}
		cut = append(cut, span{string(runes[:room]), s.style})
		cut = append(cut, span{"…", s.style})
		return cut
	}
	return append(cut, span{"…", plain})
}

// pad fills a line out to a width with spaces, which is what keeps the right
// border of a panel in one column.
func (l line) pad(width int) line {
	if gap := width - l.width(); gap > 0 {
		return append(l, span{strings.Repeat(" ", gap), plain})
	}
	return l
}

func (l line) render() string {
	var out strings.Builder
	for _, s := range l {
		if s.text == "" {
			continue
		}
		if s.style == plain {
			out.WriteString(s.text)
			continue
		}
		out.WriteString("\x1b[" + s.style + "m")
		out.WriteString(s.text)
		out.WriteString("\x1b[0m")
	}
	return out.String()
}

// statementChars is two lines of a statement, near enough. A row that grows
// without a bound pushes everything under it off the screen.
const statementChars = 190

// twoLines is a statement cut to about two lines, with the cut marked.
func twoLines(statement string) string {
	said := strings.Join(strings.Fields(statement), " ")
	if utf8.RuneCountInString(said) <= statementChars {
		return said
	}
	return strings.TrimRight(string([]rune(said)[:statementChars]), " ") + "…"
}

// wrap breaks text into at most a given number of lines of a given width. The
// last line is cut and marked when the text does not end inside the count, so
// one statement cannot take the room the panel below it needs.
func wrap(text string, width, most int) []string {
	if width < 1 || most < 1 {
		return nil
	}
	words := strings.Fields(text)
	var lines []string
	current := ""
	for len(words) > 0 {
		word := words[0]
		candidate := word
		if current != "" {
			candidate = current + " " + word
		}
		if utf8.RuneCountInString(candidate) <= width {
			current, words = candidate, words[1:]
			continue
		}
		if current != "" {
			lines = append(lines, current)
			current = ""
			if len(lines) == most {
				break
			}
			continue
		}
		// A word longer than the column has to break inside itself, or the
		// loop above would never make progress.
		runes := []rune(word)
		lines = append(lines, string(runes[:width]))
		words[0] = string(runes[width:])
		if len(lines) == most {
			break
		}
	}
	if current != "" && len(lines) < most {
		lines = append(lines, current)
		words = nil
	}
	if len(words) > 0 && len(lines) > 0 {
		last := []rune(lines[len(lines)-1])
		if len(last) >= width {
			last = last[:width-1]
		}
		lines[len(lines)-1] = strings.TrimRight(string(last), " ") + "…"
	}
	return lines
}
