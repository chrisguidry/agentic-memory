"""The pairs a merge keeps apart, whatever the similarity or the model says.

A statement never retires into praise unless it is praise itself, on any
merge. A statement retires into one that says the same thing, by the upper
cutoff or by the first question, only when that one holds all its literals.
Either merge would lose what the older statement said: a correction retired
into "nice work" is gone, and so is a URL retired into "use the example
provider". A newer statement that settles the older one in a different way
changes a value on purpose, so the second question retires the older statement
whatever literals it lacks, but only when the first question says the two are
not the same thing. A yes to both is the same thing said without the literal,
and the pair stands.
"""

from datetime import timedelta

import pytest
from _statements import ACROSS, MODEL, NOW, FakeJudge, at, held, live

from agentic_memory.merge import merge, merge_message
from agentic_memory.merge_pass import merge_backlog


async def pair(store, older: tuple[str, str], newer: tuple[str, str], similarity: float):
    """The two statements in the store, the older one said a day before."""
    first = await held(store, older[0], kind=older[1], said_at=NOW - timedelta(days=1))
    second = await held(store, newer[0], kind=newer[1], vector=at(similarity), said_at=NOW)
    return first, second


# An older statement and its kind, then a newer one of praise.
INTO_PRAISE = [
    pytest.param(
        ("Never amend a commit, make a new one.", "correction"),
        ("Nice work on the commits.", "praise"),
        id="a correction",
    ),
    pytest.param(
        ("The store is Postgres.", "semantic"),
        ("Good call, the store is Postgres.", "praise"),
        id="a fact",
    ),
]


@pytest.mark.parametrize(("older", "newer"), INTO_PRAISE)
@pytest.mark.parametrize("similarity", [0.90, 0.99])
@pytest.mark.parametrize("subject", [0, 1])
@pytest.mark.parametrize(("same", "settles"), [(True, False), (False, True)])
async def test_nothing_but_praise_retires_into_praise(
    store, older, newer, similarity, subject, same, settles
):
    ids = await pair(store, older, newer, similarity)
    judge = FakeJudge(same=same, settles=settles)
    assert await merge(store, judge, statement_id=ids[subject], model=MODEL, settings=ACROSS) == []
    assert await live(store) == {older[0], newer[0]}


@pytest.mark.parametrize(("older", "newer"), INTO_PRAISE)
async def test_the_model_is_not_asked_about_a_pair_into_praise(store, older, newer):
    ids = await pair(store, older, newer, 0.90)
    judge = FakeJudge()
    await merge(store, judge, statement_id=ids[1], model=MODEL, settings=ACROSS)
    assert judge.asked == []


# An older statement that holds a literal, then a newer one that says the same
# thing without it.
LACKING = [
    pytest.param(
        ("The player runs with --load-scripts=no --osc=no.", "procedural"),
        ("The player runs headless.", "procedural"),
        id="a flag",
    ),
    pytest.param(
        ("The provider's base URL is https://api.example.test/v1.", "semantic"),
        ("Use the example provider.", "preference"),
        id="a URL",
    ),
    pytest.param(
        ("The widget reads /etc/widget/config.toml.", "semantic"),
        ("The widget reads a config file.", "semantic"),
        id="a path",
    ),
    pytest.param(
        ("Pin the widget to 2.4.1.", "preference"),
        ("Pin the widget's version.", "preference"),
        id="a version",
    ),
    pytest.param(
        ("Call `widget.flush()` after each batch.", "procedural"),
        ("Flush the widget after each batch.", "procedural"),
        id="a name in backticks",
    ),
    pytest.param(
        ("Widget settings are in pyproject.toml.", "semantic"),
        ("Widget settings are in the project file.", "semantic"),
        id="a file name",
    ),
    pytest.param(
        ("Set WIDGET_TOKEN before the tests run.", "procedural"),
        ("Set the token before the tests run.", "procedural"),
        id="a name from the environment",
    ),
    pytest.param(
        ("The staging host is db.internal.example.test.", "semantic"),
        ("Use the staging host.", "preference"),
        id="a host name",
    ),
    pytest.param(
        ("Run the tests with -n auto.", "procedural"),
        ("Run the tests in parallel.", "procedural"),
        id="a short flag",
    ),
    pytest.param(
        ("Call merge_backlog with again set.", "procedural"),
        ("Call the backlog merge again.", "procedural"),
        id="a name from code",
    ),
    pytest.param(
        ("The database is at 10.0.0.12.", "semantic"),
        ("The database is on the private network.", "semantic"),
        id="an address",
    ),
    pytest.param(
        ("The widget's code is in acme/widget.", "semantic"),
        ("The widget's code is on the forge.", "semantic"),
        id="a repository",
    ),
    pytest.param(
        ("The widget targets python3.12.", "semantic"),
        ("The widget targets a recent Python.", "semantic"),
        id="a glued version",
    ),
]


@pytest.mark.parametrize(("older", "newer"), LACKING)
@pytest.mark.parametrize("similarity", [0.90, 0.99])
@pytest.mark.parametrize("subject", [0, 1])
@pytest.mark.parametrize("settles", [False, True])
async def test_a_statement_stands_beside_the_same_thing_without_its_literals(
    store, older, newer, similarity, subject, settles
):
    ids = await pair(store, older, newer, similarity)
    judge = FakeJudge(same=True, settles=settles)
    assert await merge(store, judge, statement_id=ids[subject], model=MODEL, settings=ACROSS) == []
    assert await live(store) == {older[0], newer[0]}


@pytest.mark.parametrize(("older", "newer"), LACKING)
async def test_the_write_keeps_the_same_thing_without_its_literals(store, older, newer):
    await pair(store, older, newer, 0.90)
    merged = await merge_message(
        store, FakeJudge(), session_id="s1", entry_id=newer[0], model=MODEL, settings=ACROSS
    )
    assert merged == 0
    assert await live(store) == {older[0], newer[0]}


@pytest.mark.parametrize(("older", "newer"), LACKING)
async def test_the_pass_keeps_the_same_thing_without_its_literals(store, older, newer):
    await pair(store, older, newer, 0.90)
    passed = await merge_backlog(settings=ACROSS, pool=store, client=FakeJudge())
    assert passed.merged == []
    assert await live(store) == {older[0], newer[0]}


# An older value and a newer one that replaces it.
SETTLED = [
    pytest.param(
        ("Pin the widget to 2.4.1.", "preference"),
        ("Pin the widget to 2.5.0.", "preference"),
        id="a version",
    ),
    pytest.param(
        ("The dev server listens on port 8080.", "semantic"),
        ("The dev server listens on port 9090.", "semantic"),
        id="a port",
    ),
    pytest.param(
        ("The config is at ~/.config/widget/a.toml.", "semantic"),
        ("The config is at ~/.config/widget/b.toml.", "semantic"),
        id="a path",
    ),
]


@pytest.mark.parametrize(("older", "newer"), SETTLED)
@pytest.mark.parametrize("similarity", [0.90, 0.99])
async def test_a_newer_value_settles_the_older(store, older, newer, similarity):
    ids = await pair(store, older, newer, similarity)
    judge = FakeJudge(same=False, settles=True)
    (merged,) = await merge(store, judge, statement_id=ids[1], model=MODEL, settings=ACROSS)
    assert merged.reason == "settles"
    assert await live(store) == {newer[0]}


# Pairs the guards let through: praise into praise, praise into another kind,
# and a survivor that holds every literal of the older statement.
MERGED = [
    pytest.param(
        ("Good call on the retry.", "praise"),
        ("Great call on the retry.", "praise"),
        id="praise into praise",
    ),
    pytest.param(
        ("Nice, the store is Postgres.", "praise"),
        ("The store is Postgres.", "semantic"),
        id="praise into a fact",
    ),
    pytest.param(
        ("The base URL is https://api.example.test/v1.", "semantic"),
        ("Use https://api.example.test/v1 for the example provider.", "preference"),
        id="a URL the survivor holds",
    ),
    pytest.param(
        ("The player runs with --osc=no.", "procedural"),
        ("The player runs with --osc=yes.", "procedural"),
        id="a flag with a new value",
    ),
]


@pytest.mark.parametrize(("older", "newer"), MERGED)
async def test_a_pair_the_guards_allow_merges(store, older, newer):
    ids = await pair(store, older, newer, 0.90)
    await merge(store, FakeJudge(), statement_id=ids[1], model=MODEL, settings=ACROSS)
    assert await live(store) == {newer[0]}
