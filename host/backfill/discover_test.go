package backfill_test

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/backfill"
)

// write makes a file and every directory above it, with content nobody reads.
func write(t *testing.T, path string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestDiscoveryFindsTheSessionFilesOfEachHarness(t *testing.T) {
	cases := []struct {
		harness string
		source  string
		files   []string
		ignored []string
	}{
		{
			harness: "claude-code",
			source:  ".claude/projects",
			files: []string{
				".claude/projects/-work-widget/22222222.jsonl",
				".claude/projects/-work-widget/aaaaaaaa.jsonl",
				".claude/projects/-work-gadget/bbbbbbbb.jsonl",
			},
			ignored: []string{".claude/projects/-work-widget/aaaaaaaa.jsonl.lock"},
		},
		{
			harness: "codex",
			source:  ".codex/sessions",
			files: []string{
				".codex/sessions/2026/09/21/rollout-cccccccc.jsonl",
				".codex/sessions/2026/09/22/rollout-dddddddd.jsonl",
			},
			ignored: []string{".codex/sessions/2026/09/22/history.json"},
		},
		{
			harness: "pi",
			source:  ".pi/agent/sessions",
			files: []string{
				".pi/agent/sessions/eeeeeeee.jsonl",
				".pi/agent/sessions/ffffffff.jsonl",
			},
			ignored: []string{".pi/agent/sessions/notes.md"},
		},
	}

	for _, test := range cases {
		t.Run(test.harness, func(t *testing.T) {
			home := t.TempDir()
			for _, name := range append(slices.Clone(test.files), test.ignored...) {
				write(t, filepath.Join(home, name))
			}

			source := backfill.Source(test.harness, home)
			if source != filepath.Join(home, test.source) {
				t.Fatalf("%s reads %s, not %s", test.harness, source, test.source)
			}

			found, err := backfill.Discover(source)
			if err != nil {
				t.Fatal(err)
			}
			want := make([]string, 0, len(test.files))
			for _, name := range test.files {
				want = append(want, filepath.Join(home, name))
			}
			slices.Sort(want)
			if !slices.Equal(found, want) {
				t.Errorf("found %v, want %v", found, want)
			}
		})
	}
}

func TestDiscoveryOfADirectoryThatIsNotThere(t *testing.T) {
	_, err := backfill.Discover(filepath.Join(t.TempDir(), "absent"))
	if err == nil {
		t.Fatal("a source that is not there discovered nothing and said nothing")
	}
}
