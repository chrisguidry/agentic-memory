package recall

import (
	"fmt"
	"strings"
	"time"
)

// Heading names what follows, so the model reads the lines as memory rather
// than as instructions from this turn.
const Heading = "Statements from this person's earlier sessions, chosen for this place and this prompt, with where each came from:"

// Block is the statements as the model reads them, one line each, with
// provenance.
//
// The statement is given as the writer wrote it. Nothing here is filtered or
// rewritten, because the point of the first injection is to watch the raw list
// land.
func Block(statements []Statement, now time.Time) string {
	lines := make([]string, 0, len(statements)+1)
	lines = append(lines, Heading)
	for _, found := range statements {
		lines = append(lines, line(found, now))
	}
	return strings.Join(lines, "\n")
}

func line(found Statement, now time.Time) string {
	where := found.ScopeKey
	if where == "" {
		where = "everywhere"
	}
	return fmt.Sprintf("- %s, %s, %s, %s: %s", found.Kind, where, who(found), age(found, now), found.Statement)
}

func who(found Statement) string {
	if found.Actor == "human" && found.ActorDepth == 0 {
		return "person"
	}
	actor := found.Actor
	if actor == "" {
		actor = "unknown"
	}
	return fmt.Sprintf("%s at depth %d", actor, found.ActorDepth)
}

func age(found Statement, now time.Time) string {
	if found.SaidAt == "" {
		return "undated"
	}
	when, err := parse(found.SaidAt)
	if err != nil {
		return "undated"
	}
	days := int(now.Sub(when) / (24 * time.Hour))
	switch {
	case days <= 0:
		return "today"
	case days == 1:
		return "1 day ago"
	default:
		return fmt.Sprintf("%d days ago", days)
	}
}

// parse reads the timestamps the service writes. A timestamp with no zone is
// read as UTC, the way the Python hook reads one.
func parse(said string) (time.Time, error) {
	if when, err := time.Parse(time.RFC3339Nano, said); err == nil {
		return when, nil
	}
	for _, layout := range []string{"2006-01-02T15:04:05.999999999", "2006-01-02T15:04:05", "2006-01-02"} {
		if when, err := time.ParseInLocation(layout, said, time.UTC); err == nil {
			return when, nil
		}
	}
	return time.Time{}, fmt.Errorf("could not read the timestamp %q", said)
}
