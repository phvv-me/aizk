import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import pytest
from doubles import deterministic_vector
from graphdb import add_member, create_group, delete_group, owned_principal
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Row, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from aizk.config import settings
from aizk.store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    acting_as,
)

DIM = settings.embed_dim


async def hybrid_recall(
    session: AsyncSession,
    qvec: list[float],
    qtext: str,
    k: int = 10,
    rrf_k: int = settings.rrf_k,
    fusion_depth: int = settings.fusion_depth,
) -> Sequence[Row]:
    """Call the migrated `hybrid_recall` SQL function on an open, principal-scoped session.

    The halfvec parameter needs its pgvector type spelled out explicitly since a bare positional
    parameter would otherwise bind as a generic array, the same seam the ORM's mapped `HALFVEC`
    columns cover automatically that a raw function call has to state by hand.

    session: open, principal-scoped session the call runs under, row level security and all.
    qvec: dense query embedding.
    qtext: lexical query text.
    k: fused chunk hits and seed facts each lane returns.
    rrf_k: reciprocal-rank-fusion damping constant.
    fusion_depth: how deep the dense and lexical chunk lanes each rank before fusion.
    """
    result = await session.execute(
        text("SELECT * FROM hybrid_recall(:qvec, :qtext, :k, :rrf_k, :fusion_depth)").bindparams(
            bindparam("qvec", type_=HALFVEC(len(qvec)))
        ),
        {"qvec": qvec, "qtext": qtext, "k": k, "rrf_k": rrf_k, "fusion_depth": fusion_depth},
    )
    return result.all()


async def seed_document(owner: uuid.UUID, scope: uuid.UUID | None = None) -> uuid.UUID:
    """Plant a bare document with a unique content hash, its id back.

    owner: principal that owns the row.
    scope: group the row is shared with, private when null.
    """
    document = uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(
            Document(id=document, content_hash=uuid.uuid4().hex, owner_id=owner, scope=scope)
        )
    return document


async def seed_chunk(
    owner: uuid.UUID,
    document: uuid.UUID,
    span: str,
    embedding: list[float] | None,
    scope: uuid.UUID | None = None,
) -> uuid.UUID:
    """Plant one chunk under a document, its id back.

    owner: principal that owns the row.
    document: parent document the chunk belongs to.
    span: chunk text, doubling as the lexical lane's match text.
    embedding: dense vector, or null to keep the chunk out of the dense lane entirely.
    scope: group the row is shared with, private when null.
    """
    chunk = uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(
            Chunk(
                id=chunk,
                document_id=document,
                ord=0,
                text=span,
                embedding=embedding,
                owner_id=owner,
                scope=scope,
            )
        )
    return chunk


async def seed_fact(
    owner: uuid.UUID,
    subject: uuid.UUID,
    statement: str,
    embedding: list[float] | None,
    scope: uuid.UUID | None = None,
) -> uuid.UUID:
    """Plant one fact naming a subject entity, its claim id back.

    owner: principal that owns the claim.
    subject: entity content id the fact is about.
    statement: the fact's natural-language statement.
    embedding: dense vector, or null to keep the fact out of both graph lanes entirely.
    scope: group the claim is shared with, private when null.
    """
    content, claim = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(
            FactContent(
                id=content,
                subject_id=subject,
                predicate="related_to",
                statement=statement,
                embedding=embedding,
            )
        )
        # a bare FK column, with no relationship() between FactContent and FactClaim, gives the
        # unit of work no dependency to sort on, so the explicit flush is what guarantees the
        # content row exists before the claim's FK insert runs.
        await session.flush()
        session.add(FactClaim(id=claim, content_id=content, owner_id=owner, scope=scope))
    return claim


async def seed_entity(owner: uuid.UUID, name: str, scope: uuid.UUID | None = None) -> uuid.UUID:
    """Plant one entity, its content id back.

    owner: principal that owns the claim.
    name: entity surface form.
    scope: group the claim is shared with, private when null.
    """
    content, claim = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(EntityContent(id=content, name=name, type="Concept"))
        await session.flush()
        session.add(EntityClaim(id=claim, content_id=content, owner_id=owner, scope=scope))
    return content


def rows_by_kind(rows: Sequence[Row], kind: str) -> list[Row]:
    """The subset of rows tagged a given kind, in the order the function returned them.

    rows: the full hybrid_recall result.
    kind: 'chunk', 'fact', or 'neighbor'.
    """
    return [row for row in rows if row.kind == kind]


def test_hybrid_recall_fuses_dense_and_lexical_chunk_ranks_by_rrf(requires_db: None) -> None:
    """A chunk both lanes rank first outscores one only the dense lane reaches, by summed RRF.

    `chunk_both` shares the exact query vector (dense rank 1) and the exact query text (lexical
    rank 1), so its score is the sum of both reciprocal ranks; `chunk_dense_only` carries no
    embedding-independent lexical match, so its embedding alone (dense rank 2, absent from the
    lexical lane since its text shares no term with the query) gives it a single reciprocal rank.
    """
    query = "alpha beta gamma"
    qvec = deterministic_vector("query:fusion", DIM)

    async def probe() -> Sequence[Row]:
        async with owned_principal() as owner:
            document = await seed_document(owner)
            both = await seed_chunk(owner, document, query, qvec)
            dense_only = await seed_chunk(
                owner, document, "unrelated span text", deterministic_vector("other", DIM)
            )
            async with acting_as(owner) as session:
                rows = await hybrid_recall(session, qvec, query, k=10)
            chunks = rows_by_kind(rows, "chunk")
            assert {row.id for row in chunks} == {both, dense_only}
            return chunks

    chunks = asyncio.run(probe())
    ranked = sorted(chunks, key=lambda row: row.score, reverse=True)
    expected_both = 2.0 / (settings.rrf_k + 1)
    expected_dense_only = 1.0 / (settings.rrf_k + 2)
    assert ranked[0].score == pytest.approx(expected_both)
    assert ranked[1].score == pytest.approx(expected_dense_only)


def test_hybrid_recall_gives_a_promoted_chunk_a_rank_bonus_over_an_equal_one(
    requires_db: None,
) -> None:
    """Two chunks tied on a single reciprocal rank still separate, the promoted one leading.

    `promoted` carries the query embedding (dense rank 1) with text sharing no lexical term, and
    `plain` carries no embedding at all so it only ever reaches the lexical lane, matched there at
    rank 1 by sharing the exact query text. Absent the trusted-first bonus the two would tie.
    """
    query = "promoted bonus probe text"
    qvec = deterministic_vector("query:promoted-bonus", DIM)

    async def probe() -> Sequence[Row]:
        async with owned_principal() as owner:
            origin = await seed_document(owner)
            promoted_document = uuid.uuid4()
            async with acting_as(owner) as session:
                session.add(
                    Document(
                        id=promoted_document,
                        content_hash=uuid.uuid4().hex,
                        owner_id=owner,
                        promoted_from=origin,
                    )
                )
            promoted = await seed_chunk(owner, promoted_document, "unrelated to the query", qvec)
            plain_document = await seed_document(owner)
            plain = await seed_chunk(owner, plain_document, query, None)
            async with acting_as(owner) as session:
                rows = await hybrid_recall(session, qvec, query, k=10)
            chunks = rows_by_kind(rows, "chunk")
            assert {row.id for row in chunks} == {promoted, plain}
            return chunks

    top, second = asyncio.run(probe())
    assert top.promoted is True
    assert second.promoted is False
    assert top.score == pytest.approx(second.score + settings.promoted_bonus)


def test_hybrid_recall_widens_a_seed_fact_to_its_one_hop_neighbor(requires_db: None) -> None:
    """The closest fact surfaces as `fact`, a fact sharing its subject as `neighbor`, k=1 apart.

    Both facts carry an embedding, so both are eligible for the dense fact lane, but `k=1` keeps
    only the closer one there; the other, excluded from `dense_fact` yet touching the same
    subject entity, is exactly what `neighbor_fact` is for.
    """
    qvec = deterministic_vector("query:neighbor", DIM)

    async def probe() -> Sequence[Row]:
        async with owned_principal() as owner:
            subject = await seed_entity(owner, "shared subject")
            await seed_fact(owner, subject, "the closest fact", qvec)
            await seed_fact(
                owner, subject, "a one-hop neighbor fact", deterministic_vector("far", DIM)
            )
            async with acting_as(owner) as session:
                rows = await hybrid_recall(session, qvec, "irrelevant text", k=1)
            return rows

    rows = asyncio.run(probe())
    [fact_row] = rows_by_kind(rows, "fact")
    [neighbor_row] = rows_by_kind(rows, "neighbor")
    assert fact_row.statement == "the closest fact"
    assert neighbor_row.statement == "a one-hop neighbor fact"


@asynccontextmanager
async def two_principals() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Yield two freshly seeded principals, each torn down independently on exit."""
    async with owned_principal() as a, owned_principal() as b:
        yield a, b


def test_hybrid_recall_never_leaks_another_principals_rows(requires_db: None) -> None:
    """Row level security still narrows the function's result, not only a hand-written query.

    Both principals plant a chunk and a fact matching the very same query vector, so absent row
    level security the two would tie for the top of every lane; each principal's own call must
    see only their own ids.
    """
    qvec = deterministic_vector("query:isolation", DIM)

    async def probe() -> tuple[Sequence[Row], Sequence[Row], set[uuid.UUID], set[uuid.UUID]]:
        async with two_principals() as (a, b):
            doc_a = await seed_document(a)
            chunk_a = await seed_chunk(a, doc_a, "isolation probe", qvec)
            entity_a = await seed_entity(a, "a subject")
            fact_a = await seed_fact(a, entity_a, "a's fact", qvec)

            doc_b = await seed_document(b)
            chunk_b = await seed_chunk(b, doc_b, "isolation probe", qvec)
            entity_b = await seed_entity(b, "b subject")
            fact_b = await seed_fact(b, entity_b, "b's fact", qvec)

            async with acting_as(a) as session:
                rows_a = await hybrid_recall(session, qvec, "isolation probe", k=10)
            async with acting_as(b) as session:
                rows_b = await hybrid_recall(session, qvec, "isolation probe", k=10)
            return rows_a, rows_b, {chunk_a, fact_a}, {chunk_b, fact_b}

    rows_a, rows_b, ids_a, ids_b = asyncio.run(probe())
    seen_a = {row.id for row in rows_a}
    seen_b = {row.id for row in rows_b}
    assert seen_a >= ids_a
    assert seen_a.isdisjoint(ids_b)
    assert seen_b >= ids_b
    assert seen_b.isdisjoint(ids_a)


def test_hybrid_recall_narrows_to_the_lens_scope(requires_db: None) -> None:
    """The app.scope lens `acting_as(principal, scope)` sets narrows the function's reach.

    A writer belongs to two groups and also owns a private chunk, all three matching the same
    query vector; the unlensed call sees all three, and lensing to one group's scope narrows the
    result to exactly that group's chunk, the same projection a scoped `acting_as` gives any other
    ORM read.
    """
    qvec = deterministic_vector("query:lens", DIM)

    async def probe() -> tuple[set[uuid.UUID], set[uuid.UUID], uuid.UUID]:
        async with owned_principal() as writer:
            group_x = await create_group(f"lens-x-{uuid.uuid4().hex[:8]}")
            group_y = await create_group(f"lens-y-{uuid.uuid4().hex[:8]}")
            await add_member(writer, group_x, role="writer")
            await add_member(writer, group_y, role="writer")
            try:
                doc_private = await seed_document(writer)
                private_chunk = await seed_chunk(writer, doc_private, "lens probe", qvec)
                doc_x = await seed_document(writer, group_x)
                chunk_x = await seed_chunk(writer, doc_x, "lens probe", qvec, group_x)
                doc_y = await seed_document(writer, group_y)
                chunk_y = await seed_chunk(writer, doc_y, "lens probe", qvec, group_y)

                async with acting_as(writer) as session:
                    unlensed = await hybrid_recall(session, qvec, "lens probe", k=10)
                async with acting_as(writer, group_x) as session:
                    lensed = await hybrid_recall(session, qvec, "lens probe", k=10)
                unlensed_ids = {row.id for row in rows_by_kind(unlensed, "chunk")}
                lensed_ids = {row.id for row in rows_by_kind(lensed, "chunk")}
                assert unlensed_ids == {private_chunk, chunk_x, chunk_y}
                return unlensed_ids, lensed_ids, chunk_x
            finally:
                await delete_group(group_x)
                await delete_group(group_y)

    unlensed_ids, lensed_ids, chunk_x = asyncio.run(probe())
    assert lensed_ids == {chunk_x}
    assert lensed_ids < unlensed_ids
