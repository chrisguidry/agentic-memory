// Package scope derives the scope a session belongs to from the directory it
// started in.
//
// Three questions are asked of each directory from the working directory up to
// the home directory.
//
//	is it a git repository?                            then it is a repo
//	is it under a forge, and does it hold repositories? then it is an org
//	is its name a domain?                              then it is a forge
//
// The levels that answer yes become the scope, joined by a slash. The home
// directory is the boundary and is never a level.
//
// Nothing is configured. Every answer comes from the filesystem, so a forge
// with no organization level and an organization that is not a repository both
// work without being described.
//
//	~/src/github.com/acme/widget         -> github.com/acme/widget    repo
//	~/src/github.com/acme                -> github.com/acme           org
//	~/src/code.example.com/widget        -> code.example.com/widget   repo
//	~/.config                            -> .config                  directory
//
// The key is a path, so retrieval inherits for free: a memory scoped to
// `github.com/acme` surfaces in any repository beneath it, and a memory
// scoped to one repository does not leak into a sibling.
package scope

import (
	"context"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
	"unicode"
)

// Of returns the scope of a directory, and which of the three questions named
// it. The answer for a directory that no question names is the top directory
// under home, with the kind "directory".
func Of(cwd, home string) (string, string) {
	cwd = resolve(cwd)
	home = resolve(home)

	// Starting at the repository root rather than the working directory is
	// what makes a session in a subdirectory scope to its repository.
	directory := cwd
	if root := repoRoot(cwd); root != "" {
		directory = resolve(root)
	}

	var kinds, names []string
	for directory != home && filepath.Dir(directory) != directory {
		name := filepath.Base(directory)
		switch {
		case isRepo(directory):
			kinds, names = append(kinds, "repo"), append(names, name)
		case isOrg(directory):
			kinds, names = append(kinds, "org"), append(names, name)
		case looksLikeDomain(name):
			kinds, names = append(kinds, "forge"), append(names, name)
		}
		directory = filepath.Dir(directory)
	}

	if len(names) > 0 {
		deepest := kinds[0]
		for left, right := 0, len(names)-1; left < right; left, right = left+1, right-1 {
			names[left], names[right] = names[right], names[left]
		}
		return strings.Join(names, "/"), deepest
	}

	relative, err := filepath.Rel(home, cwd)
	if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return filepath.Base(cwd), "directory"
	}
	if relative == "." {
		return "home", "directory"
	}
	return strings.Split(relative, string(filepath.Separator))[0], "directory"
}

// isRepo reports whether this directory is the root of a git repository.
func isRepo(path string) bool {
	_, err := os.Stat(filepath.Join(path, ".git"))
	return err == nil
}

// isOrg reports whether this directory is an organization.
//
// Holding repositories is not enough. A scratch directory and a source root
// both collect stray clones, and neither is an organization. An organization
// is a collection that belongs to a forge.
func isOrg(path string) bool {
	return looksLikeDomain(filepath.Base(filepath.Dir(path))) && holdsRepos(path)
}

// holdsRepos reports whether any immediate child of this directory is a git
// repository.
//
// The answer is read from the filesystem every time. The bastion runs for
// weeks, so an answer cached at startup would still say no after the first
// repository under a new directory is cloned.
func holdsRepos(path string) bool {
	entries, err := os.ReadDir(path)
	if err != nil {
		return false
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		if entry.Type()&fs.ModeSymlink != 0 || !entry.IsDir() {
			continue
		}
		if isRepo(filepath.Join(path, entry.Name())) {
			return true
		}
	}
	return false
}

// looksLikeDomain reports whether a directory name is a hostname, which makes
// it a forge.
//
// A leading dot is not a hostname. `~/.ai` is a directory that starts with a
// dot and happens to end in a country code, and calling it a forge would put
// every session under it in the wrong place.
func looksLikeDomain(name string) bool {
	if strings.HasPrefix(name, ".") || !strings.Contains(name, ".") {
		return false
	}
	labels := strings.Split(name, ".")
	last := labels[len(labels)-1]
	if labels[0] == "" || len(last) < 2 {
		return false
	}
	for _, letter := range last {
		if !unicode.IsLetter(letter) {
			return false
		}
	}
	return true
}

// repoRoot returns the root of the repository this directory is inside, or an
// empty string when it is inside none.
func repoRoot(cwd string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	found, err := exec.CommandContext(ctx, "git", "-C", cwd, "rev-parse", "--show-toplevel").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(found))
}

// resolve gives a path its absolute, symlink-free form. A path that does not
// exist keeps the form it was given, because the walk above home compares
// names and a missing directory still has them.
func resolve(path string) string {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return filepath.Clean(path)
	}
	real, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return absolute
	}
	return real
}
