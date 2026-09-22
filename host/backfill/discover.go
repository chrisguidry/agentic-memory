package backfill

import (
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Harnesses names the harnesses a backfill can read, in the order the help
// text lists them. It is a function rather than a variable because every
// subcommand shares this binary, and the hook's cold start pays for whatever
// a package builds at startup.
func Harnesses() []string {
	return []string{"claude-code", "codex", "pi"}
}

// Source is where a harness keeps its sessions under the home directory. This
// is all the client knows about a harness. The service reads the formats.
func Source(harness, home string) string {
	switch harness {
	case "claude-code":
		return filepath.Join(home, ".claude", "projects")
	case "codex":
		return filepath.Join(home, ".codex", "sessions")
	case "pi":
		return filepath.Join(home, ".pi", "agent", "sessions")
	}
	return ""
}

// Discover returns the session files under source, sorted. Every harness
// writes one JSON Lines file per session, so the same glob finds all three.
// The paths are sorted so two runs over one directory ship in the same order.
func Discover(source string) ([]string, error) {
	var found []string
	err := filepath.WalkDir(source, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			// A directory that cannot be read holds files this run will miss,
			// and the rest of the source is still worth shipping.
			if entry != nil && entry.IsDir() {
				return fs.SkipDir
			}
			return err
		}
		if !entry.IsDir() && strings.HasSuffix(path, ".jsonl") {
			found = append(found, path)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Strings(found)
	return found, nil
}

func home() string {
	found, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return found
}
