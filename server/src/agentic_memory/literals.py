"""The literals a statement names: the details a reader copies exactly.

A literal is a URL, a flag, a path, a file or host name, a name from code or
the environment, a number or a version, a commit hash, or a name in backticks.
A merge that retires a statement into one without its literals loses them:
"the base URL is https://api.example.test/v1" retired into "use the example
provider" leaves no statement that holds the URL.

The rules find more literals than a person would name. A literal found in
error, such as a number in ordinary prose, is a literal the survivor may lack,
and then both statements stay live, which loses nothing. A literal the rules
miss is lost in a wrong merge, so each rule matches broadly within its kind:

- a URL is a scheme and `://` up to the next space
- a long flag is `--` and a name, and its name is the literal. The value after
  `=` is left out, so `--osc=no` and `--osc=yes` both hold `--osc`
- a short flag is `-` and one letter, alone between spaces
- a path starts with `/`, `~/`, `./`, or `../`, or has a slash in it, or ends
  in a slash. `and/or`, `CI/CD`, and `24/7` are words and numbers, not paths
- a file or host name is two or more dotted parts, the last starting with a
  letter, such as `pyproject.toml` or `db.internal.example.test`. `e.g.` is
  not one, because each of its parts is one letter
- a name from the environment is capitals with an underscore, `WIDGET_TOKEN`,
  and a name from code is lowercase with an underscore, `merge_backlog`
- a commit hash is 7 to 40 hex digits with at least one letter and one digit
- a number is a run of digits and dots with no letter or digit right before
  it, other than a `v`, so `v2.4.1` gives `2.4.1` and `sha256` gives nothing.
  A version glued to a name, such as `python3.12`, gives `3.12`. `1,000` is
  `1000`, so the two spellings hold the same number
- a name in backticks is everything between the backticks, and the rules
  above also read inside the backticks

The URLs and the commit hashes are taken out of the text before the other
rules read it, so the digits inside them are not literals of their own.
"""

import re

BACKTICKED = re.compile(r"`([^`\n]+)`")
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
HASH = re.compile(r"(?<![\w.-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![\w-])(?!\.\w)")
FLAG = re.compile(r"(?<![\w-])--[A-Za-z0-9][\w-]*")
SHORT_FLAG = re.compile(r"(?<![\w-])-[A-Za-z](?![\w-])")
PATH = re.compile(r"(?<![\w/:.@+~-])[~.]{0,2}[\w.@+-]*(?:/[\w.@+-]*)+")
DOTTED = re.compile(r"(?<![\w./-])[\w-]+(?:\.[\w-]+)*\.[A-Za-z][\w-]*")
ENVIRONMENT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
GLUED_VERSION = re.compile(r"(?<![\w.])[A-Za-z]+(\d+(?:\.\d+)+)")
NUMBER = re.compile(r"(?<![\w.])v?(\d{1,3}(?:,\d{3})+(?!\d)|\d+(?:\.\d+)*)")

# A path is rooted, or has a slash between two parts, or ends in a slash. A
# pair of words or of numbers with one slash between them is prose.
ROOTED = ("/", "~/", "./", "../")
CAPITALS_OR_DIGITS = re.compile(r"[A-Z0-9]+")
PROSE = {
    "and/or",
    "either/or",
    "yes/no",
    "on/off",
    "true/false",
    "read/write",
    "input/output",
    "pass/fail",
    "start/stop",
    "open/close",
    "he/she",
    "his/her",
}

# The punctuation that ends a sentence or closes a bracket after a URL or a
# path, and a trailing slash, which names the same place with or without it.
TRAILING = ".,;:!?)]}'\"/"


def is_path(token: str) -> bool:
    """Whether a run of characters with a slash in it has the shape of a path."""
    name = token.rstrip(TRAILING)
    if not name:
        return False
    if (
        name.startswith(ROOTED)
        or name.count("/") >= 2
        or token.rstrip(".,;:!?)]}'\"").endswith("/")
    ):
        return True
    if name.lower() in PROSE:
        return False
    return not all(CAPITALS_OR_DIGITS.fullmatch(part) for part in name.split("/"))


def paths(text: str) -> set[str]:
    """The paths in text that holds no URL."""
    return {
        match.group().rstrip(TRAILING) for match in PATH.finditer(text) if is_path(match.group())
    }


def dotted(text: str) -> set[str]:
    """The file and host names, leaving out abbreviations such as `e.g`."""
    return {
        match.group()
        for match in DOTTED.finditer(text)
        if any(len(part) > 1 for part in match.group().split("."))
    }


def literals(text: str) -> set[str]:
    """Every literal a statement names."""
    urls = {match.group().rstrip(TRAILING) for match in URL.finditer(text)}
    rest = URL.sub(" ", text)
    hashes = {match.group() for match in HASH.finditer(rest)}
    rest = HASH.sub(" ", rest)
    return (
        urls
        | hashes
        | {match.group(1).strip() for match in BACKTICKED.finditer(text)}
        | {match.group() for match in FLAG.finditer(rest)}
        | {match.group() for match in SHORT_FLAG.finditer(rest)}
        | paths(rest)
        | dotted(rest)
        | {match.group() for match in ENVIRONMENT.finditer(rest)}
        | {match.group() for match in SNAKE.finditer(rest)}
        | {match.group(1) for match in GLUED_VERSION.finditer(rest)}
        | {match.group(1).replace(",", "") for match in NUMBER.finditer(rest)}
    )


def lacks(retired: str, survivor: str) -> set[str]:
    """The literals of a retired statement that its survivor does not hold."""
    return literals(retired) - literals(survivor)
