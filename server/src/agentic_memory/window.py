"""The window the classifier reads: one message, and the exchanges before it.

A prompt on its own often means little. Half the prompts in the record are
under 120 characters and most of them answer the turn before, so what is read
is the exchange: the message, and the turns between it and the prompts before
it. The two halves are kept apart, because the questions judge the message and
only use the rest to read it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import asyncpg

# Entries a harness writes for itself rather than from the conversation. Claude
# Code and pi both record injected skill text, command wrappers, interrupt
# markers, compaction summaries, and hook output as though the person had typed
# them, which is a third of everything stored as a prompt and none of what the
# person wanted. A compaction summary is the worst of these to let through: it
# is a page of the agent's own words about the whole session, and read as the
# person's it would put a preference on every sentence in it.
#
# The list is kept once, here, and the SQL below is built from it, so the
# scheduler at the door and the queries that read the record cannot disagree
# about what the person said.
PLUMBING_PREFIXES = (
    "<",
    "[Request interrupted",
    "Base directory for this skill",
    "This session is being continued from a previous conversation",
    "Stop hook feedback:",
)
NOT_PLUMBING = " AND ".join(
    f"btrim(body) NOT LIKE '{prefix.replace("'", "''")}%'" for prefix in PLUMBING_PREFIXES
)

# The prompt being read, and where it happened. A prompt with no entry id of
# its own cannot be pointed at again, and a plumbing entry is not the person.
TARGET = f"""
    SELECT occurred_at, scope_key, body, actor, actor_depth
    FROM logs
    WHERE session_id = $1 AND entry_id = $2 AND kind = 'prompt'
      AND {NOT_PLUMBING}
    LIMIT 1
"""

RECENT_PROMPTS = f"""
    SELECT occurred_at
    FROM logs
    WHERE session_id = $1 AND kind = 'prompt'
      AND occurred_at <= $2
      AND {NOT_PLUMBING}
    ORDER BY occurred_at DESC
    LIMIT $3
"""

# Everything leading up to the message, and not the message itself. The two are
# read separately because the questions judge one and only use the other.
BEFORE = """
    SELECT occurred_at, kind, body
    FROM logs
    WHERE session_id = $1 AND kind IN ('prompt', 'response')
      AND occurred_at >= $2 AND occurred_at < $3
    ORDER BY occurred_at
"""

# Where a window is cut when it is too long. An exchange starts with a speaker
# tag on its own line, so cutting here drops whole turns rather than half of
# one.
TURN = "\n\n["


@dataclass(frozen=True)
class Window:
    """One message, the exchanges before it, and where it happened."""

    message: str
    before: str
    scope_key: str | None
    actor: str | None
    actor_depth: int | None

    def state(self) -> dict[str, str]:
        """The two halves as the model reads them."""
        return {"before": self.before, "message": self.message}

    def fitted(self, budget: int) -> Window:
        """This window, cut to a number of characters the model will take.

        The provider takes about 32,000 tokens of state and questions together
        and refuses the request past that, and a message from a coding session
        can carry a pasted file or a long tool result. The message is what the
        questions judge, so it is kept whole and the exchanges before it give
        way first, oldest first, at a turn boundary. A message longer than the
        whole budget on its own is cut in the middle, because its opening says
        what it is and its ending says what was asked.
        """
        if len(self.message) >= budget:
            half = budget // 2
            left_out = len(self.message) - 2 * half
            middle = f"\n\n[... {left_out} characters left out ...]\n\n"
            return replace(
                self, message=self.message[:half] + middle + self.message[-half:], before=""
            )
        room = budget - len(self.message)
        if len(self.before) <= room:
            return self
        cut = self.before.find(TURN, len(self.before) - room)
        return replace(self, before=self.before[cut + 2 :] if cut >= 0 else "")


def plumbing(body: str) -> bool:
    """Whether a harness wrote this entry for itself rather than the person."""
    return body.lstrip().startswith(PLUMBING_PREFIXES)


def spoken(rows: Sequence[Any]) -> str:
    """The conversation in a span, as the two sides of it.

    An agent turn arrives as many records and most of them are empty, so the
    records that hold no text are dropped rather than rendered as blank turns.
    """
    lines = []
    for found in rows:
        body = (found["body"] or "").strip()
        if not body or (found["kind"] == "prompt" and plumbing(body)):
            continue
        lines.append(f"[{'person' if found['kind'] == 'prompt' else 'agent'}] {body}")
    return "\n\n".join(lines)


async def window(
    pool: asyncpg.Pool,
    session_id: str,
    entry_id: str,
    rounds: int,
) -> Window | None:
    """One message, and the exchanges leading to it.

    None comes back when the prompt is not in the record or the harness wrote
    the entry for itself, and both mean there is nothing to read.
    """
    target = await pool.fetchrow(TARGET, session_id, entry_id)
    if target is None:
        return None

    recent = await pool.fetch(RECENT_PROMPTS, session_id, target["occurred_at"], rounds)
    if not recent:
        return None

    opened = min(found["occurred_at"] for found in recent)
    before = await pool.fetch(BEFORE, session_id, opened, target["occurred_at"])
    return Window(
        message=(target["body"] or "").strip(),
        before=spoken(before),
        actor=target["actor"],
        actor_depth=target["actor_depth"],
        scope_key=target["scope_key"],
    )
