"""The statements a message may replace, put in front of the writer by number.

A message that pushes back on something is shown the statements its place
already holds, and it names the ones its new statement replaces by their
numbers in the list.
"""

import logging
from typing import Any

log = logging.getLogger("agentic_memory.replacing")

# How sure the reading has to be that a message pushes back on something before
# the statements already held about the place are put in front of the writer.
# Measured over 376 readings, this question and the correction kind together
# qualify 35 of them, which is about one message in eleven. Everything else
# writes without any candidates, so the comparison against the table costs
# nothing on the messages that cannot replace anything.
CORRECTS_EARLIER = 0.70

STANDING = """
The statements this place already holds, by number:

{standing}

If the message replaces any of them, name those numbers in "replaces" on the
statement doing the replacing. A statement replaces another when the two cannot
both be true, or when the new one settles a question the old one left open.
Adding a fact, an opinion, or a detail to one of them replaces nothing, and
naming a number ends the statement for good.
"""


def offered_numbers(
    sentence: dict[str, Any],
    candidates: dict[int, dict[str, Any]],
) -> list[int]:
    """The statements a sentence says it replaces, by their ids.

    A model answers with numbers and with the numbers as text, so both are read.
    A number that was never offered is dropped, because the model can only end a
    statement that was put in front of it, and dropping it is logged so a
    replacement that did not happen is never silent.
    """
    named = sentence.get("replaces")
    if not isinstance(named, list):
        return []
    numbers = []
    for entry in named:
        try:
            number = int(entry)
        except TypeError, ValueError:
            log.warning("the writer named %r as a replacement, which is not a number", entry)
            continue
        if number not in candidates:
            log.warning("the writer named %s as a replacement, which was not offered", number)
            continue
        numbers.append(candidates[number]["id"])
    return numbers


def candidates_block(
    candidates: dict[int, dict[str, Any]],
) -> str:
    """The held statements as the writer reads them, or nothing when there are none."""
    if not candidates:
        return ""
    listed = "\n".join(
        f"[{number}] ({row['kind']}) {row['statement']}" for number, row in candidates.items()
    )
    return STANDING.format(standing=listed)
