/**
 * The scope a session belongs to, derived from the directory it started in.
 *
 * Three questions are asked of each directory from the working directory up to
 * the home directory.
 *
 *   is it a git repository?                             then it is a repo
 *   is it under a forge, and does it hold repositories?  then it is an org
 *   is its name a domain?                               then it is a forge
 *
 * The levels that answer yes become the scope, joined by a slash. The home
 * directory is the boundary and is never a level.
 *
 * Nothing is configured. Every answer comes from the filesystem, so a forge
 * with no organization level and an organization that is not a repository both
 * work without being described.
 *
 *   ~/src/github.com/acme/widget        -> github.com/acme/widget     repo
 *   ~/src/github.com/acme               -> github.com/acme            org
 *   ~/src/code.example.com/widget       -> code.example.com/widget    repo
 *   ~/.config                           -> .config                   directory
 *
 * The key is a path, so retrieval inherits for free: a memory scoped to
 * `github.com/acme` surfaces in any repository beneath it, and a memory
 * scoped to one repository does not leak into a sibling.
 */

import { existsSync, readdirSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";

export type ScopeKind = "repo" | "org" | "forge" | "directory";

export interface Scope {
  key: string;
  kind: ScopeKind;
}

// A directory's answer to "does it hold repositories" does not change while a
// session is going, and the same directories come up on every record.
const holdsRepositories = new Map<string, boolean>();

/** Whether this directory is the root of a git repository. */
export function isRepo(path: string): boolean {
  return existsSync(join(path, ".git"));
}

/** Whether any immediate child of this directory is a git repository. */
export function holdsRepos(path: string): boolean {
  const known = holdsRepositories.get(path);
  if (known !== undefined) return known;

  let found = false;
  try {
    for (const entry of readdirSync(path, { withFileTypes: true })) {
      if (entry.name.startsWith(".") || !entry.isDirectory()) continue;
      if (existsSync(join(path, entry.name, ".git"))) {
        found = true;
        break;
      }
    }
  } catch {
    found = false;
  }

  holdsRepositories.set(path, found);
  return found;
}

/**
 * Whether a directory name is a hostname, which makes it a forge.
 *
 * A leading dot is not a hostname. `~/.ai` is a directory that starts with a
 * dot and happens to end in a country code, and calling it a forge would put
 * every session under it in the wrong place.
 */
export function looksLikeDomain(name: string): boolean {
  if (name.startsWith(".") || !name.includes(".")) return false;
  const labels = name.split(".");
  const last = labels[labels.length - 1];
  return labels[0].length > 0 && last.length >= 2 && /^[a-z]+$/i.test(last);
}

/**
 * Whether this directory is an organization.
 *
 * Holding repositories is not enough. A scratch directory and a source root
 * both collect stray clones, and neither is an organization. An organization
 * is a collection that belongs to a forge.
 */
export function isOrg(path: string): boolean {
  return looksLikeDomain(basename(dirname(path))) && holdsRepos(path);
}

/**
 * The scope of a directory, and which of the three questions named it.
 *
 * `repoRoot` is what `git rev-parse --show-toplevel` reported, when the caller
 * has already asked. Starting there rather than at the working directory is
 * what makes a session in a subdirectory scope to its repository.
 */
export function scopeOf(cwd: string, home: string, repoRoot?: string): Scope {
  const boundary = resolve(home);
  let directory = repoRoot === undefined ? resolve(cwd) : resolve(repoRoot);

  const levels: { kind: ScopeKind; name: string }[] = [];
  while (directory !== boundary) {
    const parent = dirname(directory);
    if (parent === directory) break;

    const name = basename(directory);
    if (isRepo(directory)) levels.push({ kind: "repo", name });
    else if (isOrg(directory)) levels.push({ kind: "org", name });
    else if (looksLikeDomain(name)) levels.push({ kind: "forge", name });

    directory = parent;
  }

  if (levels.length > 0) {
    levels.reverse();
    return {
      key: levels.map((level) => level.name).join("/"),
      kind: levels[levels.length - 1].kind,
    };
  }

  // Nothing answered yes, so the scope is the top directory under home. A
  // session in the home directory itself has no directory to name.
  const relativeToHome = relative(boundary, resolve(cwd));
  if (relativeToHome.startsWith("..")) {
    return { key: basename(resolve(cwd)), kind: "directory" };
  }
  const top = relativeToHome.split("/").filter(Boolean)[0];
  return { key: top ?? "home", kind: "directory" };
}
