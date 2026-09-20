"""The scope a session belongs to, derived from the directory it started in.

Three questions are asked of each directory from the working directory up to
the home directory.

    is it a git repository?                            then it is a repo
    is it under a forge, and does it hold repositories? then it is an org
    is its name a domain?                              then it is a forge

The levels that answer yes become the scope, joined by a slash. The home
directory is the boundary and is never a level.

Nothing is configured. Every answer comes from the filesystem, so a forge
with no organization level and an organization that is not a repository both
work without being described.

    ~/src/github.com/liken-sh            -> github.com/liken-sh       org
    ~/src/github.com/liken-sh/liken      -> github.com/liken-sh/liken repo
    ~/src/code.example.com/widget     -> code.example.com/widget repo
    ~/.ai                                -> .ai                      directory

The key is a path, so retrieval inherits for free: a memory scoped to
`github.com/liken-sh` surfaces in any repository beneath it, and a memory
scoped to one repository does not leak into a sibling.
"""

import os
import subprocess
from pathlib import Path

# A directory's answer to "does it hold repositories" does not change while a
# run is going, and the same directories come up once per session.
_HOLDS_REPOS: dict[Path, bool] = {}


def is_repo(path: Path) -> bool:
    """Whether this directory is the root of a git repository."""
    return (path / ".git").exists()


def holds_repos(path: Path) -> bool:
    """Whether any immediate child of this directory is a git repository."""
    if path not in _HOLDS_REPOS:
        _HOLDS_REPOS[path] = _look(path)
    return _HOLDS_REPOS[path]


def _look(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False) and (Path(entry.path) / ".git").exists():
                    return True
    except OSError:
        pass
    return False


def looks_like_domain(name: str) -> bool:
    """Whether a directory name is a hostname, which makes it a forge.

    A leading dot is not a hostname. `~/.ai` is a directory that starts with a
    dot and happens to end in a country code, and calling it a forge would put
    every session under it in the wrong place.
    """
    if name.startswith(".") or "." not in name:
        return False
    labels = name.split(".")
    return bool(labels[0]) and len(labels[-1]) >= 2 and labels[-1].isalpha()


def is_org(path: Path) -> bool:
    """Whether this directory is an organization.

    Holding repositories is not enough. A scratch directory and a source root
    both collect stray clones, and neither is an organization. An organization
    is a collection that belongs to a forge.
    """
    return looks_like_domain(path.parent.name) and holds_repos(path)


def repo_root(cwd: Path) -> Path | None:
    """The root of the repository this directory is inside, if it is inside one."""
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(done.stdout.strip()) if done.returncode == 0 else None


def scope_of(cwd: Path, home: Path | None = None) -> tuple[str, str]:
    """The scope of a directory, and which of the three questions named it."""
    cwd = Path(cwd).resolve()
    home = (home or Path.home()).resolve()

    # Starting at the repository root rather than the working directory is
    # what makes a session in a subdirectory scope to its repository.
    found = repo_root(cwd)
    directory = found if found is not None else cwd

    levels: list[tuple[str, str]] = []
    while directory != home and directory.parent != directory:
        name = directory.name
        if is_repo(directory):
            levels.append(("repo", name))
        elif is_org(directory):
            levels.append(("org", name))
        elif looks_like_domain(name):
            levels.append(("forge", name))
        directory = directory.parent

    if levels:
        levels.reverse()
        return "/".join(name for _, name in levels), levels[-1][0]

    # Nothing answered yes, so the scope is the top directory under home. A
    # session in the home directory itself has no directory to name.
    try:
        relative = cwd.relative_to(home)
    except ValueError:
        return cwd.name, "directory"
    return (relative.parts[0] if relative.parts else "home"), "directory"
