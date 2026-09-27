"""What a replay handed out, counted, and the pairs it handed.

A pair is one prompt and one statement it was handed. The pair is what a
person labels, and it outlives the code that produced it, so the report reads
every label ever given and counts the ones that match a pair this replay
handed.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import asyncpg

from ..recall import Form
from .run import Turn


class Pair(NamedTuple):
    """One prompt, named by its session and entry, and one statement it was handed."""

    session_id: str
    entry_id: str
    memory_id: int


# The labels table is written by the labelling side. A store that predates it
# has no labels, and its report shows nothing labelled.
LABELS = """
    SELECT session_id, entry_id, memory_id, label
    FROM labels
"""

HAS_LABELS = "SELECT to_regclass('labels') IS NOT NULL"


async def labels(pool: asyncpg.Pool) -> dict[Pair, str]:
    """Every label given, by the pair it judges."""
    if not await pool.fetchval(HAS_LABELS):
        return {}
    return {
        Pair(row["session_id"], row["entry_id"], row["memory_id"]): row["label"]
        for row in await pool.fetch(LABELS)
    }


def pairs(turns: Sequence[Turn]) -> list[Pair]:
    """Every pair the replay handed, in the order it handed them."""
    return [
        Pair(turn.prompt.session_id, turn.prompt.entry_id, row["id"])
        for turn in turns
        for row in turn.handout.statements
    ]


def write_pairs(path: Path, turns: Sequence[Turn]) -> None:
    """The handed pairs as sorted, tab-separated lines.

    Sorted, so two replays of one range compare with `comm` or `diff`: the
    lines only one of them has are the pairs only that version handed.
    """
    lines = sorted("\t".join(str(part) for part in pair) for pair in pairs(turns))
    path.write_text("".join(f"{line}\n" for line in lines))


@dataclass
class Counts:
    """The turns of one form, and what they were handed."""

    turns: int = 0
    handed: int = 0
    statements: int = 0


@dataclass
class Report:
    """The counts a replay is judged by."""

    turns: int = 0
    nothing: int = 0
    statements: int = 0
    unscoped: int = 0
    labelled: int = 0
    # The `opening` form is a turn of a session that was handed nothing yet,
    # whether or not the opening list is on. With the list off, its turns are
    # handed only what the match found, and a session stays in the form until
    # the match hands it something.
    forms: dict[Form, Counts] = field(default_factory=dict)
    kinds: Counter = field(default_factory=Counter)
    labels: Counter = field(default_factory=Counter)


def summarize(turns: Sequence[Turn], given: dict[Pair, str]) -> Report:
    """Count what the turns were handed, and the labels of the pairs they were handed."""
    report = Report(forms={"opening": Counts(), "match": Counts()})
    for turn in turns:
        handed = turn.handout.statements
        form = report.forms.setdefault(turn.handout.form, Counts())
        form.turns += 1
        form.handed += bool(handed)
        form.statements += len(handed)
        report.turns += 1
        report.nothing += not handed
        report.statements += len(handed)
        report.unscoped += sum(row["scope_key"] is None for row in handed)
        report.kinds.update(row["kind"] for row in handed)
    for pair in pairs(turns):
        if pair in given:
            report.labelled += 1
            report.labels[given[pair]] += 1
    return report


def share(part: int, whole: int) -> str:
    """A count beside the count it is a part of."""
    return f"{part} of {whole} ({100 * part / whole:.1f}%)" if whole else f"{part} of 0"


def render(report: Report) -> str:
    """The report as a table a person reads in a terminal."""
    per_turn = report.statements / report.turns if report.turns else 0.0
    lines = [
        f"{'turns':<24}{report.turns}",
        f"{'handed nothing':<24}{share(report.nothing, report.turns)}",
        f"{'statements':<24}{report.statements}",
        f"{'statements per turn':<24}{per_turn:.2f}",
    ]
    for name, form in report.forms.items():
        per_handed = form.statements / form.handed if form.handed else 0.0
        lines.append(
            f"{name:<24}{form.turns} turns, {form.handed} handed, {per_handed:.2f} per handed turn"
        )
    lines.append(f"{'no scope':<24}{share(report.unscoped, report.statements)}")
    lines.append("by kind")
    lines.extend(f"  {kind:<22}{count}" for kind, count in report.kinds.most_common())
    lines.append(f"{'labelled':<24}{share(report.labelled, report.statements)}")
    for label in ("good", "noise", "wrong"):
        count = report.labels[label]
        percent = f" ({100 * count / report.labelled:.1f}%)" if report.labelled else ""
        lines.append(f"  {label:<22}{count}{percent}")
    return "\n".join(lines) + "\n"
