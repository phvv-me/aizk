from typing import Protocol

from loguru import logger
from openai import APITimeoutError
from patos import FrozenModel
from pydantic import computed_field
from sqlmodel import select

from aizk.config import settings
from aizk.extract.models import Extraction
from aizk.graph.build import GraphClients
from aizk.provenance import CaptureContext
from aizk.serving.gate import GateClient
from aizk.store import Chunk
from aizk.store.identity import User
from aizk.types import Scopes


class ExtractionService(Protocol):
    """The extraction capability a gate replay needs."""

    async def extract(self, text: str) -> Extraction:
        """Extract one graph slice from source text."""
        ...


class GateReport(FrozenModel):
    """The build gate replayed against forced extraction, its savings versus its losses."""

    chunks: int
    accepted: int
    rejected: int
    rejected_with_facts: int
    facts_lost: int
    timed_out: int

    @computed_field
    def positive_rate(self) -> float:
        """The fraction of gated chunks the gate lets through to extraction."""
        return self.accepted / self.chunks if self.chunks else 0.0

    @computed_field
    def false_negative_rate(self) -> float:
        """The fraction of rejected chunks whose forced extraction still finds facts."""
        return self.rejected_with_facts / self.rejected if self.rejected else 0.0

    def render(self) -> str:
        """One scorecard line, extraction calls saved against the facts that cost."""
        return (
            f"gate replay n={self.chunks} accepted={self.accepted} rejected={self.rejected} "
            f"positive_rate={self.positive_rate:.3f} "
            f"false_negative_rate={self.false_negative_rate:.3f} "
            f"facts_lost={self.facts_lost} timed_out={self.timed_out}"
        )


async def gated_chunks(scopes: Scopes, limit: int | None) -> list[Chunk]:
    """The stored chunks of one exact scope set in id order, the gate replay's population."""
    key = frozenset(scopes)
    async with User.system(key) as session:
        return list(
            await session.exec(
                select(Chunk).where(Chunk.scopes == sorted(key)).order_by(Chunk.id).limit(limit)
            )
        )


async def measure_gate(
    scopes: Scopes | None = None,
    limit: int | None = 50,
    extractor: ExtractionService | None = None,
) -> GateReport:
    """Replay the relevance gate over stored chunks and force-extract the rejected ones.

    Counts the extraction calls the gate saves (its rejections) against the facts a
    forced extraction still finds inside those rejections, the gate's false-negative
    cost. Opt-in only, never part of a build, since it spends one bounded model call per
    rejected chunk.

    scopes: the exact corpus scope set, the system scope when null.
    limit: how many stored chunks to replay at most, null for all of them.
    """
    key = frozenset(scopes or (settings.system_user_id,))
    extractor = extractor or GraphClients.from_settings(settings).extractor
    chunks = [
        chunk
        for chunk in await gated_chunks(key, limit)
        if len(chunk.text.strip()) >= settings.extract_min_chars
    ]
    accepted = rejected = rejected_with_facts = facts_lost = timed_out = 0
    for chunk in chunks:
        if await GateClient.from_settings(settings).relevant(chunk.text):
            accepted += 1
            continue
        rejected += 1
        capture = CaptureContext.model_validate(chunk.provenance)
        try:
            extraction = await extractor.extract(capture.search_text(chunk.text))
        except APITimeoutError:
            timed_out += 1
            continue
        facts_lost += len(extraction.facts)
        rejected_with_facts += bool(extraction.facts)
    report = GateReport(
        chunks=len(chunks),
        accepted=accepted,
        rejected=rejected,
        rejected_with_facts=rejected_with_facts,
        facts_lost=facts_lost,
        timed_out=timed_out,
    )
    logger.info("gate replay over {n} chunks: {report}", n=len(chunks), report=report.render())
    return report
