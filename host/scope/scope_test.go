package scope_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/scope"
)

// tree builds the directories the package docstring names, under one home.
// A `.git` directory that git itself would reject is enough for the
// filesystem questions, and the one real repository proves the git fallback.
func tree(t *testing.T) string {
	t.Helper()
	home := filepath.Join(t.TempDir(), "home")
	for _, made := range []string{
		"src/example.test/acme/widget/.git",
		"src/example.test/acme/gadget/.git",
		"src/code.example.test/widget/.git",
		"src/loose/clone/.git",
		".config/agentic-memory",
	} {
		if err := os.MkdirAll(filepath.Join(home, made), 0o755); err != nil {
			t.Fatal(err)
		}
	}

	repository := filepath.Join(home, "src", "example.test", "acme", "doohickey")
	if err := os.MkdirAll(filepath.Join(repository, "host", "scope"), 0o755); err != nil {
		t.Fatal(err)
	}
	made := exec.Command("git", "init", "--quiet", repository)
	if out, err := made.CombinedOutput(); err != nil {
		t.Fatalf("git init: %v: %s", err, out)
	}
	return home
}

func TestTheScopeOfADirectory(t *testing.T) {
	home := tree(t)
	for _, found := range []struct {
		directory string
		key       string
		kind      string
	}{
		{"src/example.test/acme/widget", "example.test/acme/widget", "repo"},
		{"src/example.test/acme", "example.test/acme", "org"},
		{"src/example.test", "example.test", "forge"},
		{"src/code.example.test/widget", "code.example.test/widget", "repo"},
		// `loose` holds a clone and no forge names it, so it is not an
		// organization and the clone under it stands alone.
		{"src/loose", "src", "directory"},
		{"src/loose/clone", "clone", "repo"},
		{".config", ".config", "directory"},
		{".config/agentic-memory", ".config", "directory"},
		{"", "home", "directory"},
		// Inside a real repository, the scope is the repository's, not the
		// subdirectory's.
		{"src/example.test/acme/doohickey/host/scope", "example.test/acme/doohickey", "repo"},
	} {
		t.Run(found.directory, func(t *testing.T) {
			key, kind := scope.Of(filepath.Join(home, found.directory), home)
			if key != found.key || kind != found.kind {
				t.Errorf("got %q %q, want %q %q", key, kind, found.key, found.kind)
			}
		})
	}
}

func TestADirectoryOutsideHomeIsNamedByItself(t *testing.T) {
	home := tree(t)
	elsewhere := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.MkdirAll(elsewhere, 0o755); err != nil {
		t.Fatal(err)
	}
	key, kind := scope.Of(elsewhere, home)
	if key != "elsewhere" || kind != "directory" {
		t.Errorf("got %q %q, want %q %q", key, kind, "elsewhere", "directory")
	}
}
