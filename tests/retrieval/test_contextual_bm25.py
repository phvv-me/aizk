import asyncio

import pytest
from graphdb import owned_principal
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aizk.config import settings
from aizk.extract.ingest import ingest_text
from aizk.store import Chunk, acting_as

# a distinctive title word absent from the body, so a lexical match on it can only come from the
# contextual preamble the ingest prepends, never from the chunk's own text
TITLE_WORD = "zephyrine"
BODY = "the passage discusses apples and oranges and nothing else at all"

# the vchord_bm25 index and offline tokenizer the 0001 migration names, mirrored here since a raw
# probe of the bm25 lane has to spell out the same names the migration's DDL created
BM25_INDEX = "ix_chunk_bm25"
BM25_TOKENIZER = "aizk_bm25"


async def bm25_matches(session: AsyncSession, query: str) -> list[str]:
    """Rank chunks by the migrated vchord_bm25 lane directly, the same query bm25_chunks once ran.

    session: open, principal-scoped session.
    query: natural-language search string.
    """
    rows = await session.execute(
        text(
            "SELECT text FROM ("
            " SELECT c.text AS text,"
            f" c.bm25 <&> to_bm25query('{BM25_INDEX}', tokenize(:query, '{BM25_TOKENIZER}'))"
            " AS rank FROM chunk c ORDER BY rank LIMIT 5"
            ") ranked WHERE ranked.rank < 0"
        ),
        {"query": query},
    )
    return [row.text for row in rows]


async def tsvector_matches(session: AsyncSession, query: str) -> list[str]:
    """Rank chunks by the portable ts_rank lane directly, the same query ts_rank_chunks once ran.

    session: open, principal-scoped session.
    query: natural-language search string.
    """
    tsquery = func.plainto_tsquery("english", query)
    rows = await session.execute(
        select(Chunk.text)
        .where(Chunk.__table__.c.tsv.bool_op("@@")(tsquery))
        .order_by(func.ts_rank(Chunk.tsv, tsquery).desc())
        .limit(5)
    )
    return list(rows.scalars())


async def matches(contextual_bm25: bool, backend: str) -> list[str]:
    """Ingest one document then rank the configured lexical lane for the title-only word.

    contextual_bm25: whether the situating preamble reaches the lexical field for this run.
    backend: the lexical lane to probe, vchord_bm25 for the migrated default or tsvector for the
        portable generated column every backend carries.
    """
    patch = pytest.MonkeyPatch()
    patch.setattr(settings, "contextual_bm25", contextual_bm25)
    try:
        async with owned_principal() as owner:
            await ingest_text(BODY, title=TITLE_WORD, owner_id=owner)
            async with acting_as(owner) as session:
                lane = bm25_matches if backend == "vchord_bm25" else tsvector_matches
                return await lane(session, TITLE_WORD)
    finally:
        patch.undo()


@pytest.mark.usefixtures("fake_embedder")
@pytest.mark.parametrize("backend", ["vchord_bm25", "tsvector"], ids=["bm25", "tsvector"])
def test_contextual_preamble_reaches_the_lexical_field(requires_db: None, backend: str) -> None:
    """With contextual bm25 on, the title-only word matches the chunk on both lexical lanes.

    The chunk body never carries the title word, so a lexical hit on it proves the situating
    preamble reached the lexical field the bm25 trigger and the tsvector both read, while the
    displayed chunk text stays the raw body. The vchord_bm25 probe only runs when the live stack
    was actually migrated with that backend, since the bm25 column only exists there.
    """
    if backend == "vchord_bm25" and settings.bm25_backend != "vchord_bm25":
        pytest.skip("stack migrated without the vchord_bm25 lane")

    texts = asyncio.run(matches(True, backend))

    assert texts == [BODY]


@pytest.mark.usefixtures("fake_embedder")
@pytest.mark.parametrize("backend", ["vchord_bm25", "tsvector"], ids=["bm25", "tsvector"])
def test_without_contextual_the_title_word_never_matches(requires_db: None, backend: str) -> None:
    """With the flag off the lexical field is the raw body, so the title word finds nothing."""
    if backend == "vchord_bm25" and settings.bm25_backend != "vchord_bm25":
        pytest.skip("stack migrated without the vchord_bm25 lane")

    texts = asyncio.run(matches(False, backend))

    assert texts == []
