import uuid

import dbutil
from doubles import FakeLLM, RecordingEmbedder
from patos import FrozenModel

from aizk.graph.tier_builder import TierBuilder


class TinyReport(FrozenModel):
    """A minimal tier report whose `lines` are the texts a builder embeds.

    lines: the report texts to embed, empty to exercise the nothing-to-embed short-circuit.
    """

    lines: list[str]


class RecordingTier(TierBuilder[str, TinyReport]):
    """A concrete `TierBuilder` recording each template step, driving `build` without a database.

    grounding: the value `gather` returns, None to exercise the nothing-to-run short-circuit.
    """

    def __init__(self, principal_id: uuid.UUID, grounding: str | None) -> None:
        super().__init__(principal_id, "system prompt", TinyReport)
        self.grounding = grounding
        self.body_calls: list[str] = []
        self.upsert_calls: list[tuple[str, TinyReport, list[list[float]]]] = []

    async def gather(self) -> str | None:
        return self.grounding

    def body(self, grounding: str) -> str:
        self.body_calls.append(grounding)
        return f"user turn for {grounding}"

    def texts(self, report: TinyReport) -> list[str]:
        return report.lines

    async def upsert(self, grounding: str, report: TinyReport, vectors: list[list[float]]) -> int:
        self.upsert_calls.append((grounding, report, vectors))
        return len(vectors)


def test_build_short_circuits_when_there_is_nothing_to_gather(
    fake_embedder: RecordingEmbedder, fake_llm: FakeLLM
) -> None:
    """A null `gather` returns zero and never calls the model, the empty-tier short-circuit."""

    async def body() -> int:
        return await RecordingTier(uuid.uuid4(), grounding=None).build()

    assert dbutil.run(body()) == 0
    assert fake_embedder.calls == []  # never embedded
    assert fake_llm.completions.calls == []  # and never summarized


def test_build_short_circuits_when_the_report_has_no_texts(
    fake_embedder: RecordingEmbedder, fake_llm: FakeLLM
) -> None:
    """A report with no texts to embed returns zero after summarizing, never touching the embedder.

    `gather` runs and the structured summary is asked for, but an empty `texts` stops the pass
    before any embedding or upsert, so a tier that finds a theme yet nothing worth storing is a
    no-op rather than an empty write.
    """
    fake_llm.register(TinyReport, TinyReport(lines=[]))

    async def body() -> tuple[int, list[str], int]:
        tier = RecordingTier(uuid.uuid4(), grounding="a cluster")
        written = await tier.build()
        return written, tier.body_calls, len(tier.upsert_calls)

    written, body_calls, upsert_count = dbutil.run(body())
    assert written == 0
    assert body_calls == ["a cluster"]  # the grounding was rendered into the summary turn
    assert fake_embedder.calls == [] and upsert_count == 0  # but nothing embedded or written


def test_build_runs_the_full_gather_summarize_embed_store_pipeline(
    fake_embedder: RecordingEmbedder, fake_llm: FakeLLM
) -> None:
    """The full path embeds one vector per report text and upserts them, returning the row count.

    The template threads the gathered grounding and the summarized report into `upsert` alongside
    one embedding per text, so a two-line report writes two rows off two document-lane embeddings.
    """
    fake_llm.register(TinyReport, TinyReport(lines=["first theme", "second theme"]))

    async def body() -> tuple[int, list[tuple[list[str], str]], int]:
        tier = RecordingTier(uuid.uuid4(), grounding="a cluster")
        written = await tier.build()
        grounding, report, vectors = tier.upsert_calls[0]
        assert grounding == "a cluster" and report.lines == ["first theme", "second theme"]
        return written, fake_embedder.calls, len(vectors)

    written, calls, vector_count = dbutil.run(body())
    assert written == 2  # one row per text
    assert calls == [(["first theme", "second theme"], "document")]  # one batched document embed
    assert vector_count == 2  # one embedding threaded into upsert per text
