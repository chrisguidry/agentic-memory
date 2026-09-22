// Package repository reads what git says about a directory, so a record
// carries the repository, the branch, and the revision the work happened on.
package repository

import (
	"context"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Repository is what git answers about one directory. Every field is
// optional, because a directory outside a repository answers none of it and a
// repository with no remote answers some.
type Repository struct {
	Name     string `json:"name,omitempty"`
	Owner    string `json:"owner,omitempty"`
	URL      string `json:"url,omitempty"`
	Branch   string `json:"branch,omitempty"`
	Revision string `json:"revision,omitempty"`
}

// Reader answers for a directory once and remembers the answer.
//
// The answer is cached for the bastion's lifetime. The branch and the revision
// do move while a session runs, and reading them per shipment would run five
// git processes per event on the path that is supposed to be cheap.
type Reader struct {
	seen sync.Map // cwd -> *Repository
}

// Of returns what git says about a directory, or nothing when git says
// nothing about it.
func (r *Reader) Of(cwd string) *Repository {
	if cwd == "" {
		return nil
	}
	if found, ok := r.seen.Load(cwd); ok {
		return found.(*Repository)
	}
	found := read(cwd)
	r.seen.Store(cwd, found)
	return found
}

func read(cwd string) *Repository {
	root := git(cwd, "rev-parse", "--show-toplevel")
	remote := git(cwd, "config", "--get", "remote.origin.url")
	owner, named := RemoteParts(remote)

	found := Repository{
		Name:     named,
		Owner:    owner,
		URL:      remote,
		Branch:   git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
		Revision: git(cwd, "rev-parse", "HEAD"),
	}
	if root != "" {
		found.Name = filepath.Base(root)
	}
	if found == (Repository{}) {
		return nil
	}
	return &found
}

// RemoteParts returns the owner and the repository name inside a git remote
// URL.
//
// Both shapes appear: `git@host:owner/name.git` and `https://host/owner/name`.
// A URL with no owner gives the name alone.
func RemoteParts(url string) (string, string) {
	if url == "" {
		return "", ""
	}
	path := url
	if scheme := strings.Index(url, "://"); scheme >= 0 {
		path = url[scheme+3:]
	} else if at := strings.Index(url, "@"); at >= 0 {
		if colon := strings.Index(url, ":"); colon >= 0 {
			path = url[colon+1:]
		}
	}
	var segments []string
	for _, segment := range strings.Split(path, "/") {
		if segment != "" {
			segments = append(segments, segment)
		}
	}
	if len(segments) == 0 {
		return "", ""
	}
	named := strings.TrimSuffix(segments[len(segments)-1], ".git")
	if len(segments) >= 2 {
		return segments[len(segments)-2], named
	}
	return "", named
}

// git runs one command and returns what it printed, or an empty string when it
// failed. A git that hangs on a network mount holds a shipment, so every call
// has a deadline.
func git(cwd string, arguments ...string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	found, err := exec.CommandContext(ctx, "git", append([]string{"-C", cwd}, arguments...)...).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(found))
}
