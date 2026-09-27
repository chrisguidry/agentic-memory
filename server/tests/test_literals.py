"""The literals a statement names, which a merge must not lose.

A literal is a detail a reader copies rather than paraphrases, such as a URL, a
flag, a path, a number, or a name in backticks. A statement that holds one
retires into a statement that says the same thing only when that statement
holds it too.
"""

import pytest

from agentic_memory.literals import lacks, literals


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Run the tests before a commit.", set()),
        # URLs
        ("The base URL is https://api.example.test/v1.", {"https://api.example.test/v1"}),
        ("Docs live at (http://docs.example.test/).", {"http://docs.example.test"}),
        # Flags, long and short
        (
            "The player runs with --load-scripts=no --osc=no.",
            {"--load-scripts", "--osc"},
        ),
        ("Pass -v to see more, and -n auto.", {"-v", "-n"}),
        ("A well-known name, and a long-running job - done.", set()),
        # Paths
        ("The data directory is /srv/widget/.", {"/srv/widget"}),
        ("Keys are in ~/.config/widget.", {"~/.config/widget"}),
        ("The entry point is src/widget/app.py.", {"src/widget/app.py"}),
        ("The unit tests are in server/tests/unit.", {"server/tests/unit"}),
        ("The code is in src/app/ and server/.", {"src/app", "server"}),
        ("The widget's code is in acme/widget.", {"acme/widget"}),
        ("Read the docs and/or the CI/CD notes.", set()),
        ("Use HTTP/2, TCP/IP, and I/O, 24/7.", {"2", "24", "7"}),
        # File names and host names
        ("Settings are in pyproject.toml and README.md.", {"pyproject.toml", "README.md"}),
        ("The staging host is db.internal.example.test.", {"db.internal.example.test"}),
        ("Pick a name, e.g. a short one, i.e. a word.", set()),
        # Names from code and the environment
        ("Set WIDGET_TOKEN before the tests run.", {"WIDGET_TOKEN"}),
        ("Call merge_backlog with again set.", {"merge_backlog"}),
        ("Write the README in PLAIN English, snake case or not.", set()),
        # Numbers and versions
        ("The service listens on 8080.", {"8080"}),
        ("Pin Python 3.12 and widget v2.4.1.", {"3.12", "2.4.1"}),
        ("The model is bge-small-en-v1.5.", {"1.5"}),
        ("The widget targets python3.12.", {"3.12"}),
        ("Use k8s and sha256.", set()),
        ("The limit is 1,000 sessions, not 3,4.", {"1000", "3", "4"}),
        # Commit hashes
        ("The fix is in 33fb346.", {"33fb346"}),
        ("A decade of deadbeef.", set()),
        # Backticks
        ("Call `widget.flush()` after each batch.", {"widget.flush()", "widget.flush"}),
        ("Set `--depth=1` on the clone.", {"--depth=1", "--depth", "1"}),
    ],
)
def test_the_literals_of_a_statement(text, found):
    assert literals(text) == found


@pytest.mark.parametrize(
    ("retired", "survivor", "lacking"),
    [
        # A survivor that states the detail again, or states it in more words,
        # loses nothing.
        (
            "The base URL is https://api.example.test/v1.",
            "Use https://api.example.test/v1 as the base URL of the example provider.",
            set(),
        ),
        ("Run the tests before a commit.", "Always run the tests before a commit.", set()),
        ("The limit is 1,000 sessions.", "Keep at most 1000 sessions.", set()),
        # A general survivor loses the specifics.
        (
            "The base URL is https://api.example.test/v1.",
            "Use the example provider.",
            {"https://api.example.test/v1"},
        ),
        (
            "The player runs with --load-scripts=no --osc=no.",
            "The player runs headless.",
            {"--load-scripts", "--osc"},
        ),
        ("The player runs with --osc=no.", "The player runs with --osc=yes.", set()),
        ("The service listens on 8080.", "The service listens on 9090.", {"8080"}),
        (
            "Call `widget.flush()` after each batch.",
            "Flush the widget.",
            {"widget.flush()", "widget.flush"},
        ),
    ],
)
def test_what_a_survivor_lacks(retired, survivor, lacking):
    assert lacks(retired, survivor) == lacking
