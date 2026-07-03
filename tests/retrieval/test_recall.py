import asyncio
import uuid
from pathlib import Path
from typing import Protocol

import pytest
from graphdb import owned_principal

from aizk.cli import migrate
from aizk.config import settings
from aizk.extract.ingest import ingest_path
from aizk.retrieval import FactHit, Hit, RecallResult, graph_search, recall, search


class RecordingEmbedder(Protocol):
    """The recording embedder double's surface this module reads, the calls it logged."""

    calls: list[tuple[list[str], str]]


class RecordingReranker(Protocol):
    """The recording reranker double's surface, installed by the fixture for the rerank lane."""

    calls: list[tuple[str, list[str]]]


@pytest.mark.parametrize(
    ("query", "query_routing"),
    [
        ("When was Alice born", False),
        ("When was Alice born", True),
        ("How is Alice related to Bob", True),
        ("Give me an overview of the project", True),
    ],
)
def test_recall_runs_every_route_end_to_end_over_the_fake_seam(
    query: str,
    query_routing: bool,
    fake_embedder: RecordingEmbedder,
    fake_reranker: RecordingReranker,
    requires_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh principal recalls an empty graph through the real lanes, only the seam faked.

    Driving every route against a principal that owns no rows exercises recall's routing, the
    assemble and gap-fill rounds, and the community and RAPTOR lanes without an internal mock,
    the embedder and reranker swapped at the Seam the way production selects a backend.
    """
    principal = uuid.uuid4()

    monkeypatch.setattr(settings, "query_routing", query_routing)
    monkeypatch.setattr(settings, "ppr", True)
    monkeypatch.setattr(settings, "raptor", True)
    result = asyncio.run(recall(query, principal_id=principal, k=4))

    assert isinstance(result, RecallResult)
    assert result.query == query
    assert result.as_of is None
    assert all(isinstance(hit, Hit) for hit in result.hits)
    assert all(isinstance(fact, FactHit) for fact in result.facts)
    assert ([query], "query") in fake_embedder.calls


def test_search_and_graph_search_return_lanes_over_the_fake_seam(
    fake_embedder: RecordingEmbedder,
    fake_reranker: RecordingReranker,
    requires_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two thin search entrypoints embed the query once and return their typed lane."""
    principal = uuid.uuid4()

    monkeypatch.setattr(settings, "rerank", False)
    hits = asyncio.run(search("alpha", k=4, principal_id=principal))
    facts = asyncio.run(graph_search("alpha", k=4, principal_id=principal))

    assert all(isinstance(hit, Hit) for hit in hits)
    assert all(isinstance(fact, FactHit) for fact in facts)
    assert ("alpha", "query") in [(texts[0], mode) for texts, mode in fake_embedder.calls]


def test_recall_fuses_real_ingested_chunks(
    tmp_path: Path,
    requires_db: None,
    fake_embedder: RecordingEmbedder,
    fake_reranker: RecordingReranker,
) -> None:
    """End to end against a live Postgres, recall surfaces the chunk it just ingested.

    The lone integration happy path, proving the seam wires up against a live Postgres, with the
    embedder and reranker faked since every model-shaped step now lives in a container this suite
    never starts. The flow runs under a fresh owned principal so row level security keeps residue
    from other runs out of the ranked pool, and the principal's rows are purged on exit.
    """
    migrate()
    marker = uuid.uuid4().hex
    note = tmp_path / f"note-{marker}.md"
    note.write_text(f"Alpha beta gamma {marker} over here.\n\nDelta epsilon zeta over there.\n")

    async def flow() -> RecallResult:
        async with owned_principal() as owner:
            assert await ingest_path(note, owner_id=owner) > 0
            return await recall(f"alpha beta gamma {marker}", principal_id=owner, k=4)

    result = asyncio.run(flow())
    assert isinstance(result, RecallResult)
    assert result.hits
    assert any(marker in hit.text for hit in result.hits)
