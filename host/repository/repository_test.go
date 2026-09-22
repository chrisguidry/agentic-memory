package repository_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/chrisguidry/agentic-memory/host/repository"
)

const remote = "git@example.test:acme/widget.git"

// checkout makes a repository with one commit on a named branch and a remote,
// so every field git can answer has an answer.
func checkout(t *testing.T) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), "widget")
	if err := os.MkdirAll(filepath.Join(root, "host"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "host", "notes.txt"), []byte("a line\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, arguments := range [][]string{
		{"init", "--quiet", "--initial-branch", "main"},
		{"config", "user.email", "someone@example.test"},
		{"config", "user.name", "Someone"},
		{"config", "commit.gpgsign", "false"},
		{"remote", "add", "origin", remote},
		{"add", "."},
		{"commit", "--quiet", "-m", "the first commit"},
	} {
		run := exec.Command("git", append([]string{"-C", root}, arguments...)...)
		if out, err := run.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v: %s", arguments, err, out)
		}
	}
	return root
}

func TestWhatGitSaysAboutACheckout(t *testing.T) {
	root := checkout(t)
	reader := &repository.Reader{}

	found := reader.Of(filepath.Join(root, "host"))
	if found == nil {
		t.Fatal("got no repository for a checkout")
	}
	if found.Name != "widget" {
		t.Errorf("got name %q, want %q", found.Name, "widget")
	}
	if found.Owner != "acme" {
		t.Errorf("got owner %q, want %q", found.Owner, "acme")
	}
	if found.URL != remote {
		t.Errorf("got url %q, want %q", found.URL, remote)
	}
	if found.Branch != "main" {
		t.Errorf("got branch %q, want %q", found.Branch, "main")
	}
	if len(found.Revision) != 40 && len(found.Revision) != 64 {
		t.Errorf("got revision %q, want a full object name", found.Revision)
	}
}

func TestADirectoryOutsideARepositoryHasNone(t *testing.T) {
	reader := &repository.Reader{}
	if found := reader.Of(t.TempDir()); found != nil {
		t.Errorf("got %+v, want nothing", found)
	}
}

func TestTheOwnerAndNameInsideARemote(t *testing.T) {
	for _, found := range []struct {
		url   string
		owner string
		name  string
	}{
		{"git@example.test:acme/widget.git", "acme", "widget"},
		{"https://example.test/acme/widget", "acme", "widget"},
		{"https://example.test/acme/widget.git", "acme", "widget"},
		// A URL with one segment before the name gives that segment as the
		// owner, which is what the Python client sent.
		{"https://example.test/widget.git", "example.test", "widget"},
		{"/srv/git/widget.git", "git", "widget"},
		{"", "", ""},
	} {
		t.Run(found.url, func(t *testing.T) {
			owner, name := repository.RemoteParts(found.url)
			if owner != found.owner || name != found.name {
				t.Errorf("got %q %q, want %q %q", owner, name, found.owner, found.name)
			}
		})
	}
}
