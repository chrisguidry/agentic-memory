"""The probe route: what a turn would be handed, with nothing recorded.

A validator drives sessions in clones of real repositories to judge recall,
and nothing it types may become part of the person's history. A probe has a
route of its own, because a service that predates probes ignores a field on
`/recall` that it does not name, and records the turn. The same service
answers this path with 404, so the probe gets no memory and nothing is stored.
"""

from fastapi import APIRouter, Request
from pydantic import Field

from . import metrics, recall
from .settings import get_settings

router = APIRouter()


class ProbeAsk(recall.Ask):
    """What a probe session says about the turn that is starting.

    `seen` is what the client handed this session on its earlier probes. The
    service records nothing for a probe, so the client's list is the only
    record of it. A probe carries no count of missed deadlines, so the live
    count holds only what a person's sessions missed.
    """

    seen: list[int] = Field(default_factory=list, max_length=10000)


@router.post("/recall/probe")
async def probe_turn(request: Request, ask: ProbeAsk) -> dict:
    """What a turn would be handed, chosen on the live path and recorded nowhere."""
    with metrics.phase("request"):
        handout = await recall.turn(
            request.app.state.pool,
            request.app.state.embedder,
            get_settings(),
            session_id=ask.session_id,
            harness=ask.harness,
            scope_key=ask.scope_key,
            prompt=ask.prompt,
            probe=recall.Probe(frozenset(ask.seen)),
        )
    return recall.answer(handout)
