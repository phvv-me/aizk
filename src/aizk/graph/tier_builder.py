import uuid
from abc import ABC, abstractmethod

from patos import FrozenModel

from ..extract.llm import structured
from ..serving import Embedder


class TierBuilder[GroundingT, ReportT: FrozenModel](ABC):
    """Template method for a graph tier's report pass: gather, summarize, embed, then store.

    Every tier (community, RAPTOR rollup, profile, insight) grounds an LLM call in graph facts,
    embeds the report, and writes it back; `build` is that shape end to end, so it lives once here
    instead of copy-pasted with slight variations across every tier's own module. `gather` may read
    material a caller already fetched under RLS on this instance's behalf, the way a per-cluster or
    per-level pass shares one read across many builder instances.

    principal_id: identity that owns the written rows and whose visibility scopes the grounding.
    """

    def __init__(
        self, principal_id: uuid.UUID, system_prompt: str, report_type: type[ReportT]
    ) -> None:
        self.principal_id = principal_id
        self.system_prompt = system_prompt
        self.report_type = report_type
        self.embedder = Embedder()

    @abstractmethod
    async def gather(self) -> GroundingT | None:
        """The material this run grounds its call in, null when there is nothing to run on."""

    @abstractmethod
    def body(self, grounding: GroundingT) -> str:
        """Render the gathered grounding into the structured call's user turn."""

    @abstractmethod
    def texts(self, report: ReportT) -> list[str]:
        """The report text or texts to embed, one for a single summary, several for a batch."""

    @abstractmethod
    async def upsert(
        self, grounding: GroundingT, report: ReportT, vectors: list[list[float]]
    ) -> int:
        """Write the report and its embeddings back, one per text; return how many rows written."""

    async def build(self) -> int:
        """Gather, summarize, embed, and store one tier pass; return how many rows were written.

        Skips the model calls entirely when there is nothing to gather or nothing to embed.
        """
        grounding = await self.gather()
        if grounding is None:
            return 0
        report = await structured(self.system_prompt, self.body(grounding), self.report_type)
        texts = self.texts(report)
        if not texts:
            return 0
        vectors = await self.embedder.embed(texts, mode="document")
        return await self.upsert(grounding, report, vectors)
