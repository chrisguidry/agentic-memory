// Package terminal draws to a terminal without holding one open: styled text,
// panels, and the escape sequences that clear a screen and hide its caret.
//
// It is written as ANSI select-graphic-rendition parameters rather than
// through a terminal library, because the binary is shared with the Claude
// Code hook and a library would add package initialization to every turn.
//
// `top` and `label` both draw one screen from whatever the bastion last
// answered, and both cut long text to fit a box instead of letting the
// terminal wrap it past the edge a person is reading. That is the one domain
// this package covers: everything here is about drawing, and nothing in it
// knows what a memory or a label is.
package terminal

import (
	"fmt"
	"io"
	"os"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
	"unsafe"
)

// Styles, and the escape sequences a frame is drawn with.
const (
	Plain     = ""
	Dim       = "2"
	Bold      = "1"
	Red       = "31"
	Green     = "32"
	Yellow    = "33"
	Blue      = "34"
	Magenta   = "35"
	Cyan      = "36"
	White     = "37"
	HomeClear = "\x1b[H\x1b[2J"
	HideCaret = "\x1b[?25l"
	ShowCaret = "\x1b[?25h"
)

// KindStyle is the colour a kind of statement is drawn in. It is a function
// rather than a map so that nothing in this package runs before main does.
func KindStyle(kind string) string {
	switch kind {
	case "semantic":
		return Cyan
	case "procedural":
		return Green
	case "prospective":
		return Yellow
	case "preference":
		return Magenta
	case "correction":
		return Red
	case "praise":
		return Blue
	}
	return White
}

// Kinds is the order the classifier's kinds are compared in. Two kinds with
// the same probability resolve to the one named first, the way the Python
// does.
func Kinds() []string {
	return []string{"semantic", "procedural", "prospective", "preference", "correction", "praise"}
}

// Span is a run of text in one style.
type Span struct {
	Text  string
	Style string
}

// Line is text with its styles, measured in characters rather than bytes, so
// a statement with accented letters still fits the column it is drawn in.
type Line []Span

func (l Line) Width() int {
	total := 0
	for _, s := range l {
		total += utf8.RuneCountInString(s.Text)
	}
	return total
}

// Truncate cuts a line to a width and marks the cut, so a long statement ends
// at the panel's edge instead of wrapping past it.
func (l Line) Truncate(width int) Line {
	if l.Width() <= width || width < 1 {
		return l
	}
	room := width - 1
	var cut Line
	for _, s := range l {
		runes := []rune(s.Text)
		if len(runes) <= room {
			cut = append(cut, s)
			room -= len(runes)
			continue
		}
		cut = append(cut, Span{string(runes[:room]), s.Style})
		cut = append(cut, Span{"…", s.Style})
		return cut
	}
	return append(cut, Span{"…", Plain})
}

// Pad fills a line out to a width with spaces, which is what keeps the right
// border of a panel in one column.
func (l Line) Pad(width int) Line {
	if gap := width - l.Width(); gap > 0 {
		return append(l, Span{strings.Repeat(" ", gap), Plain})
	}
	return l
}

func (l Line) Render() string {
	var out strings.Builder
	for _, s := range l {
		if s.Text == "" {
			continue
		}
		if s.Style == Plain {
			out.WriteString(s.Text)
			continue
		}
		out.WriteString("\x1b[" + s.Style + "m")
		out.WriteString(s.Text)
		out.WriteString("\x1b[0m")
	}
	return out.String()
}

// TwoLineChars is about two lines of a statement. A row that grows without a
// bound pushes everything under it off the screen.
const TwoLineChars = 190

// TwoLines is text cut to about two lines, with the cut marked.
func TwoLines(text string) string {
	said := strings.Join(strings.Fields(text), " ")
	if utf8.RuneCountInString(said) <= TwoLineChars {
		return said
	}
	return strings.TrimRight(string([]rune(said)[:TwoLineChars]), " ") + "…"
}

// Wrap breaks text into at most a given number of lines of a given width. The
// last line is cut and marked when the text does not end inside the count, so
// one statement cannot take the room the panel below it needs.
func Wrap(text string, width, most int) []string {
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

// Plural names a count of things the way a person writes it.
func Plural(count int, thing string) Line {
	word := thing
	if count != 1 {
		word += "s"
	}
	return Line{{fmt.Sprintf("%d %s", count, word), Dim}}
}

// Panel is the box one list, or one pair, is drawn in.
type Panel struct {
	Title    Line
	Subtitle Line
	Border   string
	Body     []Line
}

// Draw returns the panel's rows, with the title in the top border and the
// subtitle in the bottom, both centred.
func (p Panel) Draw(width int) []string {
	rows := []string{border("╭", "╮", p.Title, p.Border, width)}
	for _, body := range p.Body {
		row := Line{{"│ ", p.Border}}
		row = append(row, body.Truncate(width-4).Pad(width-4)...)
		row = append(row, Span{" │", p.Border})
		rows = append(rows, row.Render())
	}
	return append(rows, border("╰", "╯", p.Subtitle, p.Border, width))
}

func border(left, right string, label Line, style string, width int) string {
	inner := width - 2
	if label.Width() > 0 {
		label = append(Line{{" ", Plain}}, append(label.Truncate(inner-4), Span{" ", Plain})...)
	}
	before := (inner - label.Width()) / 2
	after := inner - label.Width() - before
	row := Line{{left, style}, {strings.Repeat("─", max(before, 0)), style}}
	row = append(row, label...)
	row = append(row, Span{strings.Repeat("─", max(after, 0)), style}, Span{right, style})
	return row.Render()
}

// Cut trims rows to the height a terminal has, so a frame longer than the
// terminal does not scroll the rows above it out of view.
func Cut(rows []string, height int) []string {
	if height > 0 && len(rows) > height {
		return rows[:height]
	}
	return rows
}

// Ago is how long ago a moment was, in as few characters as it takes.
func Ago(when string, now time.Time) string {
	found, ok := moment(when)
	if !ok {
		return ""
	}
	seconds := int(now.Sub(found).Seconds())
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
		if found, err := time.Parse(layout, when); err == nil {
			return found.UTC(), true
		}
	}
	return time.Time{}, false
}

// Size is the terminal a frame is drawn into.
type Size struct {
	Rows    int
	Columns int
}

// fallbackRows and fallbackColumns are the terminal a program that is not
// attached to one gets.
const (
	fallbackRows    = 24
	fallbackColumns = 80
)

// Detect returns the size of the terminal a writer is attached to. A writer
// that is not a terminal, such as a file redirected to, gets the fallback.
func Detect(out io.Writer) Size {
	file, ok := out.(*os.File)
	if !ok {
		return Size{Rows: fallbackRows, Columns: fallbackColumns}
	}
	var window struct{ rows, columns, width, height uint16 }
	_, _, errno := syscall.Syscall(
		syscall.SYS_IOCTL,
		file.Fd(),
		syscall.TIOCGWINSZ,
		uintptr(unsafe.Pointer(&window)),
	)
	if errno != 0 || window.rows == 0 || window.columns == 0 {
		return Size{Rows: fallbackRows, Columns: fallbackColumns}
	}
	return Size{Rows: int(window.rows), Columns: int(window.columns)}
}
