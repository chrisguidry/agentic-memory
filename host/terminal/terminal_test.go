package terminal

import (
	"strings"
	"testing"
	"time"
)

func TestATextIsCutToAboutTwoLines(t *testing.T) {
	cases := []struct {
		name string
		text string
		want string
	}{
		{"short text is unchanged", "keep the socket warm", "keep the socket warm"},
		{"whitespace collapses", "keep  the\n socket\twarm", "keep the socket warm"},
		{"empty text stays empty", "   ", ""},
		{
			"text at the limit is unchanged",
			strings.Repeat("a", TwoLineChars),
			strings.Repeat("a", TwoLineChars),
		},
		{
			"text over the limit is cut and marked",
			strings.Repeat("a", TwoLineChars+20),
			strings.Repeat("a", TwoLineChars) + "…",
		},
		{
			"a cut in a space drops the space",
			strings.Repeat("a", TwoLineChars-1) + " word",
			strings.Repeat("a", TwoLineChars-1) + "…",
		},
	}
	for _, cut := range cases {
		t.Run(cut.name, func(t *testing.T) {
			if got := TwoLines(cut.text); got != cut.want {
				t.Errorf("TwoLines(%q) = %q, want %q", cut.text, got, cut.want)
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
	for _, found := range cases {
		t.Run(found.name, func(t *testing.T) {
			if got := Ago(found.when, now); got != found.want {
				t.Errorf("Ago(%q) = %q, want %q", found.when, got, found.want)
			}
		})
	}
}

func TestWrapBreaksAtWordsAndMarksWhatIsLeftOut(t *testing.T) {
	cases := []struct {
		name        string
		text        string
		width, most int
		want        []string
	}{
		{"text under the width is one line", "keep it warm", 20, 2, []string{"keep it warm"}},
		{
			"text over the width breaks between words",
			"keep the socket warm",
			10, 2,
			[]string{"keep the", "socket…"},
		},
		{
			"a line past the count is cut and marked",
			"one two three four five six",
			8, 2,
			[]string{"one two", "three…"},
		},
		{"no room draws nothing", "anything", 0, 2, nil},
	}
	for _, found := range cases {
		t.Run(found.name, func(t *testing.T) {
			got := Wrap(found.text, found.width, found.most)
			if len(got) != len(found.want) {
				t.Fatalf("Wrap(%q, %d, %d) = %q, want %q", found.text, found.width, found.most, got, found.want)
			}
			for i := range got {
				if got[i] != found.want[i] {
					t.Errorf("Wrap(%q, %d, %d)[%d] = %q, want %q", found.text, found.width, found.most, i, got[i], found.want[i])
				}
			}
		})
	}
}

func TestALineIsMeasuredInCharactersNotBytes(t *testing.T) {
	line := Line{{"café", Plain}}
	if line.Width() != 4 {
		t.Errorf("Width() = %d, want 4", line.Width())
	}
}

func TestATruncatedLineMarksTheCut(t *testing.T) {
	line := Line{{"a long statement", Plain}}
	got := line.Truncate(6).Render()
	if got != "a lon…" {
		t.Errorf("Truncate(6) rendered %q", got)
	}
}

func TestAPaddedLineFillsToTheWidth(t *testing.T) {
	line := Line{{"hi", Plain}}
	if got := line.Pad(5).Render(); got != "hi   " {
		t.Errorf("Pad(5) rendered %q", got)
	}
}

func TestAPanelDrawsATitledBox(t *testing.T) {
	panel := Panel{
		Title:    Line{{"title", Bold}},
		Subtitle: Plural(1, "row"),
		Border:   Green,
		Body:     []Line{{{"one row", Plain}}},
	}
	rows := panel.Draw(20)
	if len(rows) != 3 {
		t.Fatalf("Draw(20) drew %d rows, want 3", len(rows))
	}
	if !strings.Contains(rows[0], "title") {
		t.Errorf("the top border did not carry the title:\n%s", rows[0])
	}
	if !strings.Contains(rows[len(rows)-1], "1 row") {
		t.Errorf("the bottom border did not carry the subtitle:\n%s", rows[len(rows)-1])
	}
}

func TestKindStyleNamesAColourForEveryClassifiedKind(t *testing.T) {
	for _, kind := range Kinds() {
		if KindStyle(kind) == "" {
			t.Errorf("KindStyle(%q) has no colour", kind)
		}
	}
	if KindStyle("an-unknown-kind") == "" {
		t.Error("an unknown kind should still get a colour")
	}
}
