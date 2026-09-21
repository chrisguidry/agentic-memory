"""Merging statements that say the same thing.

The table holds one rule many times in different words, and the match cannot
pass a rule that is its own baseline. A statement is compared with its nearest
live neighbours of the same kind in the same scope, and the ones that say the
same thing retire into the one said most recently through the pointer plan 03
built. Nothing is deleted or rewritten.

Two statements at or above the upper cutoff are one sentence with a word moved,
and they merge on the number alone. Between the upper and the lower cutoff an
embedding cannot tell a negation from its opposite, so the System One model is
asked whether the two agree. Below the lower cutoff they are different enough
that nothing is compared.
"""

import logging

import asyncpg
from docket import Depends, Shared
from typesafe_sdk import AsyncTypeSafeClient, Noul

from .classify import model_client
from .db import store_pool
from .memories import retire
from .settings import Settings, get_settings

log = logging.getLogger("agentic_memory.merge")

# The question the classifier's System One model answers about a pair. It is one
# yes/no proposition, so it needs no threshold per kind: the model's probability
# is read as a yes above one half.
SAME = Noul(
    instructions={
        "question": (
            "Do `first` and `second` say the same thing, and does neither one "
            "forbid what the other allows?"
        ),
        "inspect": "`first` and `second`",
        "focus": (
            "Two statements say the same thing when a reader who believes one is "
            "right to believe the other. One may add a detail. One may not permit "
            "what the other forbids, and one may not be the negation of the other."
        ),
    },
    criteria={
        "true": "They agree, and neither permits what the other forbids.",
        "false": "They differ in what they require, permit, or forbid.",
    },
)

# The point on the model's probability above which an answer is read as a yes.
YES = 0.5

# One live statement embedded with the model being compared, with its kind, its
# scope, and the moment it was said. A statement with no moment cannot be placed
# in order, so it is never merged.
SUBJECT = """
    SELECT id, statement, kind, scope_key, said_at
    FROM memories
    WHERE id = $1
      AND superseded_by IS NULL
      AND embedding IS NOT NULL
      AND embedding_model = $2
      AND said_at IS NOT NULL
"""

# The live neighbours of that statement: the same kind, the same scope, embedded
# with the same model, at or above the lower cutoff, nearest first. A null scope
# is its own scope, because "tests come before code" in one project and in
# another are one rule stated twice and merging them would move where the rule
# applies.
NEIGHBOURS = """
    SELECT m.id, m.statement, m.kind, m.scope_key, m.said_at,
           1 - (m.embedding <=> s.embedding) AS similarity
    FROM memories m
    JOIN memories s ON s.id = $1
    WHERE m.superseded_by IS NULL
      AND m.id <> $1
      AND m.kind = $2
      AND m.scope_key IS NOT DISTINCT FROM $3
      AND m.embedding IS NOT NULL
      AND m.embedding_model = $4
      AND 1 - (m.embedding <=> s.embedding) >= $5
    ORDER BY m.embedding <=> s.embedding
"""

# Every live statement of this model that could still be merged, oldest first.
# Oldest first means that when a group is reached, its newest statement is the
# survivor, and the older ones retire into it.
BACKLOG = """
    SELECT m.id
    FROM memories m
    WHERE m.superseded_by IS NULL
      AND m.embedding IS NOT NULL
      AND m.embedding_model = $1
      AND m.said_at IS NOT NULL
    ORDER BY m.said_at ASC, m.id ASC
"""


async def agrees(client: AsyncTypeSafeClient, first: str, second: str) -> bool:
    """Whether the model says two statements say the same thing."""
    response = await client.system_one(
        state={"first": first, "second": second},
        questions={"same": SAME},
    )
    return response.nouls["same"].noul >= YES


async def merge(
    pool: asyncpg.Pool,
    client: AsyncTypeSafeClient,
    *,
    statement_id: int,
    model: str,
    settings: Settings,
) -> list[int]:
    """Merge the near-duplicates of one statement, and say which ones ended.

    The statement and the neighbours that say the same thing are one group, and
    the group's most recently said member is the survivor. Every other member
    takes the pointer to the survivor, so the ones merged in one pass point
    straight at it rather than at each other.
    """
    subject = await pool.fetchrow(SUBJECT, statement_id, model)
    if subject is None:
        return []

    neighbours = await pool.fetch(
        NEIGHBOURS,
        statement_id,
        subject["kind"],
        subject["scope_key"],
        model,
        settings.merge_lower,
    )

    group = [dict(subject)]
    for neighbour in neighbours:
        same = neighbour["similarity"] >= settings.merge_upper or await agrees(
            client, subject["statement"], neighbour["statement"]
        )
        if same:
            group.append(dict(neighbour))

    if len(group) == 1:
        return []

    survivor = max(group, key=lambda row: (row["said_at"], row["id"]))
    replaced = [row["id"] for row in group if row["id"] != survivor["id"]]
    ended = await retire(pool, replaced=replaced, replacement=survivor["id"])
    if ended:
        log.info("merged %s statements into %s", len(ended), survivor["id"])
    return ended


# The live statements one message wrote, which is what a write merges after it
# embeds them. The model is the one being compared, so a statement embedded
# before a model change is not merged until it is embedded again.
WRITTEN = """
    SELECT m.id
    FROM memories m
    WHERE m.session_id = $1
      AND m.entry_id = $2
      AND m.superseded_by IS NULL
      AND m.embedding IS NOT NULL
      AND m.embedding_model = $3
    ORDER BY m.id
"""


async def merge_message(
    pool: asyncpg.Pool,
    client: AsyncTypeSafeClient,
    *,
    session_id: str,
    entry_id: str,
    model: str,
    settings: Settings,
) -> int:
    """Merge what one message wrote, after its statements have been embedded."""
    written = await pool.fetch(WRITTEN, session_id, entry_id, model)
    merged = 0
    for row in written:
        merged += len(
            await merge(pool, client, statement_id=row["id"], model=model, settings=settings)
        )
    return merged


async def merge_backlog(
    *,
    settings: Settings,
    pool: asyncpg.Pool,
    client: AsyncTypeSafeClient,
) -> int:
    """Merge every live statement that has not been merged, oldest first.

    This is the pass that clears what is already in the table. It runs once as
    a task the service can schedule, and the same code runs on every write
    after that.
    """
    found = await pool.fetch(BACKLOG, settings.embed_model)
    merged = 0
    for row in found:
        merged += len(
            await merge(
                pool,
                client,
                statement_id=row["id"],
                model=settings.embed_model,
                settings=settings,
            )
        )
    log.info("merged %s statements over the backlog", merged)
    return merged


async def merge_statements(
    *,
    settings: Settings = Depends(get_settings),
    pool: asyncpg.Pool = Shared(store_pool),
    client: AsyncTypeSafeClient = Shared(model_client),
) -> None:
    """Merge the near-duplicates already in the table, as a task."""
    await merge_backlog(settings=settings, pool=pool, client=client)
