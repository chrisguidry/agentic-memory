"""Judging one turn's handout.

A label is not about the whole handout, it is about one pair: one prompt and
one statement the turn was given. That is why an injection knows nothing
about labelling, and why a sample can be redrawn against a store that has
moved on without disturbing a label already given. The pair outlives the
rules that produced it.

`sample` draws a sample of pairs from the record and stores it under a name.
`pairs` walks a sample a pair at a time and takes a person's judgment of each
one. `context` answers for the exchanges before a pair's prompt, for a
person who needs to see more of the session than the pair alone shows.
"""

from fastapi import APIRouter

from .context import router as context_router
from .pairs import router as pairs_router
from .sample import main

__all__ = ["router", "main"]

router = APIRouter()
router.include_router(pairs_router)
router.include_router(context_router)
