import asyncio
import uuid

import httpx
import pytest
from doubles import deterministic_vector
from graphdb import FakeLLM, owned_principal
from openai import APITimeoutError
from sqlalchemy import func, select

from aizk.config import settings
from aizk.extract.models import (
    ConsolidationVerdict,
    ExtractedEntity,
    ExtractedFact,
    Extraction,
    TimedFact,
)
from aizk.graph import build as build_module
from aizk.graph.build import GraphWriter, build_graph, dedup_entities, redirect_entity
from aizk.graph.ids import entity_id
from aizk.store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    LiveFact,
    acting_as,
)


async def seed_chunk(owner: uuid.UUID, text: str) -> uuid.UUID:
    """Plant a document and one pending chunk the build extracts a graph slice from.

    owner: principal that owns the document and chunk.
    text: the span text the chunk carries.
    """
    document, chunk = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(Document(id=document, content_hash=uuid.uuid4().hex, owner_id=owner))
        session.add(Chunk(id=chunk, document_id=document, ord=0, text=text, owner_id=owner))
    return chunk


@pytest.mark.usefixtures("fake_embedder")
def test_build_graph_writes_a_slice_then_skips_the_built_chunk(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """The first pass writes the extracted entity and fact, the second finds the chunk done.

    A chunk counts as pending until a fact records it as its source, so a build resumed after the
    slice landed reprocesses nothing and reports a zero delta.
    """
    owner = fresh_principal
    fake_llm.completions.responses[Extraction] = Extraction(
        entities=[ExtractedEntity(name="Ada", type="Author")],
        facts=[ExtractedFact(subject="Ada", predicate="related_to", statement="Ada keeps notes")],
    )

    async def probe() -> tuple[tuple[int, int], tuple[int, int]]:
        await seed_chunk(owner, "Ada keeps notes about memory")
        first = await build_graph(principal_id=owner)
        second = await build_graph(principal_id=owner)
        return first, second

    (entities_added, facts_added), second = asyncio.run(probe())
    assert entities_added >= 1
    assert facts_added >= 1
    assert second == (0, 0)


def test_redirect_entity_covers_the_null_absent_and_dropped_cases() -> None:
    """A null id, an id absent from the map, and one mapped to a replacement or to null.

    A pure function, tested directly without a database round trip: null passes through
    unchanged (a unary fact's absent object), an id the merge never touched also passes through
    unchanged, and an id present in the map resolves to its canonical replacement or, mapped to
    null, reports the drop `repoint_fact_content` reads to delete the dangling fact outright.
    """
    canonical, duplicate, dropped, untouched = (uuid.uuid4() for _ in range(4))
    redirect = {duplicate: canonical, dropped: None}

    assert redirect_entity(redirect, None) == (None, False)
    assert redirect_entity(redirect, untouched) == (untouched, False)
    assert redirect_entity(redirect, duplicate) == (canonical, False)
    assert redirect_entity(redirect, dropped) == (None, True)


async def seed_duplicate_entities(owner: uuid.UUID) -> uuid.UUID:
    """Plant two distinct entity content rows folding to one key plus a fact on one, return the
    fact's content id.

    The duplicate carries a fresh content id, the shape the fuzzy matcher leaves when it mints a
    second node for a near-duplicate name rather than reusing the content-addressed one, so dedup
    has two real content rows to merge under the same normal form. Each content row needs a claim
    of its own before it is visible to anyone, `owner`'s stake on both.

    owner: principal that owns the claims.
    """
    canonical = entity_id("Team Memory", "Concept")
    duplicate = uuid.uuid4()
    fact_content_id = uuid.uuid4()
    fact_claim_id = uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(
            EntityContent(id=canonical, name="Team Memory", type="Concept", embedding=None)
        )
        session.add(
            EntityContent(id=duplicate, name="team-memory", type="Concept", embedding=None)
        )
        # a bare FK column, unlike a declared relationship(), gives the unit of work no reason to
        # order these two mapper groups, so an explicit flush is what makes the content rows exist
        # before the claims that reference them are inserted in the same transaction.
        await session.flush()
        session.add(EntityClaim(content_id=canonical, owner_id=owner))
        session.add(EntityClaim(content_id=duplicate, owner_id=owner))
        await session.flush()
        session.add(
            FactContent(
                id=fact_content_id,
                subject_id=duplicate,
                predicate="related_to",
                statement="the duplicate carries a fact",
                embedding=None,
            )
        )
        await session.flush()
        session.add(FactClaim(id=fact_claim_id, content_id=fact_content_id, owner_id=owner))
    return fact_content_id


@pytest.mark.usefixtures("fake_embedder")
def test_dedup_merges_a_slug_twin_and_then_converges(
    fresh_principal: uuid.UUID,
) -> None:
    """Two slug spellings of one thing merge to a single node, and a rerun merges nothing more.

    The duplicate's fact content is repointed onto the surviving content before the duplicate is
    deleted, so the second pass finds one canonical node and is a no-op, the idempotence a
    scheduled rebuild needs, and the surviving node is exactly the one the repointed fact content
    now names.
    """
    owner = fresh_principal

    async def probe() -> tuple[int, int, int, bool]:
        fact_content_id = await seed_duplicate_entities(owner)
        first = await dedup_entities(principal_id=owner)
        second = await dedup_entities(principal_id=owner)
        async with acting_as(owner) as session:
            survivors = list(await session.scalars(select(EntityContent.id)))
            subject = await session.scalar(
                select(FactContent.subject_id).where(FactContent.id == fact_content_id)
            )
        return first, second, len(survivors), subject == survivors[0]

    first, second, survivors, fact_points_at_survivor = asyncio.run(probe())
    assert first == 1
    assert second == 0
    assert survivors == 1
    assert fact_points_at_survivor is True


@pytest.mark.usefixtures("fake_embedder")
def test_dedup_drops_a_path_like_entity() -> None:
    """An entity whose name folds to nothing was a path the extractor mistook for a thing, gone."""

    async def probe() -> int:
        async with owned_principal() as owner:
            content_id = uuid.uuid4()
            async with acting_as(owner) as session:
                session.add(
                    EntityContent(
                        id=content_id, name="notes/graph_rag.md", type="Concept", embedding=None
                    )
                )
                await session.flush()
                session.add(EntityClaim(content_id=content_id, owner_id=owner))
            merged = await dedup_entities(principal_id=owner)
            async with acting_as(owner) as session:
                remaining = await session.scalar(select(func.count()).select_from(EntityContent))
            return (merged or 0) * 10 + (remaining or 0)

    assert asyncio.run(probe()) == 1 * 10 + 0


@pytest.mark.usefixtures("fake_embedder")
def test_dedup_drops_a_dangling_fact_naming_a_dropped_duplicate() -> None:
    """A fact whose subject was a dropped path-like entity is dangling and removed outright.

    The object leg names an ordinary, never-duplicate entity, so the same pass also exercises the
    pass-through branch that leaves an unaffected id untouched while the subject leg drops the
    fact, rather than repointing it to nothing.
    """

    async def probe() -> tuple[int, int]:
        async with owned_principal() as owner:
            path_like, ordinary, fact_content_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            async with acting_as(owner) as session:
                session.add(
                    EntityContent(
                        id=path_like, name="notes/graph_rag.md", type="Concept", embedding=None
                    )
                )
                session.add(
                    EntityContent(
                        id=ordinary, name="Ordinary Node", type="Concept", embedding=None
                    )
                )
                await session.flush()
                session.add(EntityClaim(content_id=path_like, owner_id=owner))
                session.add(EntityClaim(content_id=ordinary, owner_id=owner))
                session.add(
                    FactContent(
                        id=fact_content_id,
                        subject_id=path_like,
                        object_id=ordinary,
                        predicate="related_to",
                        statement="a dangling fact naming a dropped duplicate",
                        embedding=None,
                    )
                )
                await session.flush()
                session.add(FactClaim(content_id=fact_content_id, owner_id=owner))
            await dedup_entities(principal_id=owner)
            async with acting_as(owner) as session:
                remaining_facts = await session.scalar(
                    select(func.count()).select_from(FactContent)
                )
                remaining_entities = await session.scalar(
                    select(func.count()).select_from(EntityContent)
                )
            return remaining_facts or 0, remaining_entities or 0

    remaining_facts, remaining_entities = asyncio.run(probe())
    assert remaining_facts == 0  # the dangling fact is dropped, not repointed to nothing
    assert remaining_entities == 1  # only the ordinary, never-duplicate entity survives


async def seed_titled_chunk(owner: uuid.UUID, title: str) -> None:
    """Plant a titled document with one pending chunk, the source the title filter selects on.

    owner: principal that owns the document and chunk.
    title: the document title the build's source substring matches against.
    """
    document, chunk = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(
            Document(id=document, content_hash=uuid.uuid4().hex, owner_id=owner, title=title)
        )
        session.add(Chunk(id=chunk, document_id=document, ord=0, text="a span", owner_id=owner))


@pytest.mark.usefixtures("fake_embedder")
def test_build_graph_filters_source_drops_paths_and_skips_unresolved(
    fresh_principal: uuid.UUID, fake_llm: FakeLLM
) -> None:
    """The source filter selects the titled chunk, a path-name entity drops, a ghost subject skips.

    The slice repeats one entity name so the second resolve reuses the content-addressed node, it
    names a path the extractor mistook for a thing so it never becomes a node, and emits a fact
    whose subject resolves to nothing so it is skipped, leaving one entity and one resolvable fact.
    """
    owner = fresh_principal
    fake_llm.completions.responses[Extraction] = Extraction(
        entities=[
            ExtractedEntity(name="Ada", type="Author"),
            ExtractedEntity(name="Ada", type="Author"),
            ExtractedEntity(name="notes/graph_rag.md", type="Concept"),
        ],
        facts=[
            ExtractedFact(subject="Ada", predicate="related_to", statement="Ada keeps notes"),
            ExtractedFact(
                subject="ghost", predicate="related_to", statement="ghost is unresolved"
            ),
        ],
    )

    async def probe() -> tuple[int, int]:
        await seed_titled_chunk(owner, "alpha source")
        return await build_graph(principal_id=owner, source="alpha")

    entities_added, facts_added = asyncio.run(probe())
    assert entities_added == 1
    assert facts_added == 1


@pytest.mark.usefixtures("fake_embedder")
def test_build_graph_leaves_a_chunk_pending_when_extraction_times_out(
    fresh_principal: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out extraction abandons the chunk and writes nothing, so a later run retries it."""
    owner = fresh_principal

    async def time_out(text: str) -> Extraction:
        raise APITimeoutError(request=httpx.Request("POST", "http://llm.invalid"))

    monkeypatch.setattr(build_module, "extract_graph", time_out)

    async def probe() -> tuple[int, int]:
        await seed_titled_chunk(owner, "beta source")
        return await build_graph(principal_id=owner)

    assert asyncio.run(probe()) == (0, 0)


@pytest.mark.usefixtures("fake_embedder")
def test_consolidate_skips_a_vanished_supersedes_and_caches_the_review_stamp(
    fresh_principal: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An UPDATE naming a fact no longer present inserts cleanly, and one writer stamps once.

    The verdict is faked to supersede an id no row carries, so the retirement lookup comes back
    empty and the insert proceeds untouched, the race-safe defense against a stale supersession
    target. Consolidating a second fact through the same writer reuses the review stamp resolved on
    the first, the per-writer cache, so both statements land as two live claims.
    """
    owner = fresh_principal
    phantom = uuid.uuid4()

    async def phantom_update(fact: TimedFact, existing: list[LiveFact]) -> ConsolidationVerdict:
        return ConsolidationVerdict(action="UPDATE", supersedes=phantom)

    monkeypatch.setattr(build_module, "decide_consolidation", phantom_update)

    async def probe() -> int:
        subject = uuid.uuid4()
        async with acting_as(owner) as session:
            session.add(EntityContent(id=subject, name="Subject", type="Concept"))
            await session.flush()
            session.add(EntityClaim(content_id=subject, owner_id=owner))
        chunk = await seed_chunk(owner, "a span")
        async with acting_as(owner) as session:
            writer = GraphWriter(session, owner, None)
            for statement in ("first claim", "second claim"):
                await writer.consolidate(
                    TimedFact(subject="Subject", predicate="related_to", statement=statement),
                    chunk,
                )
        async with acting_as(owner) as session:
            return await session.scalar(
                select(func.count())
                .select_from(FactClaim)
                .join(FactContent, FactContent.id == FactClaim.content_id)
                .where(FactContent.subject_id.is_not(None))
                .execution_options(**{settings.skip_live_gate: True})
            )

    assert asyncio.run(probe()) == 2


@pytest.mark.usefixtures("fake_embedder")
@pytest.mark.parametrize("scenario", ["insert", "exact", "fuzzy", "path"])
def test_resolve_entity_inserts_reuses_fuzzy_matches_or_drops(
    fresh_principal: uuid.UUID, scenario: str
) -> None:
    """Resolution mints a new node, reuses the exact one, folds a near-match, or drops a path-slug.

    insert: an unseen name mints a content-addressed node and a second resolve reuses it. exact: a
    name whose content id is already stored returns it without a fuzzy search. fuzzy: a fresh name
    whose embedding equals a stored entity's vector resolves onto that neighbor under the cosine
    threshold. path: a name that normalizes to nothing folds away with a null return.
    """
    owner = fresh_principal

    async def probe() -> uuid.UUID | None:
        async with acting_as(owner) as session:
            if scenario == "exact":
                content_id = entity_id("Exact Resolve Fixture", "Author")
                session.add(
                    EntityContent(
                        id=content_id,
                        name="Exact Resolve Fixture",
                        type="Author",
                        embedding=None,
                    )
                )
                await session.flush()
                session.add(EntityClaim(content_id=content_id, owner_id=owner))
            if scenario == "fuzzy":
                # the stored node carries the exact vector the embedder mints for "Newcomer", so a
                # different content id still lands within the resolution threshold of this neighbor
                content_id = entity_id("Existing", "Concept")
                session.add(
                    EntityContent(
                        id=content_id,
                        name="Existing",
                        type="Concept",
                        embedding=deterministic_vector("document:Newcomer", settings.embed_dim),
                    )
                )
                await session.flush()
                session.add(EntityClaim(content_id=content_id, owner_id=owner))
        async with acting_as(owner) as session:
            writer = GraphWriter(session, owner, None)
            if scenario == "insert":
                first = await writer.resolve("Brand New", "Concept")
                second = await writer.resolve("Brand New", "Concept")
                assert first == second == entity_id("Brand New", "Concept")
                return first
            if scenario == "exact":
                return await writer.resolve("Exact Resolve Fixture", "Author")
            if scenario == "fuzzy":
                return await writer.resolve("Newcomer", "Concept")
            return await writer.resolve("notes/graph_rag.md", "Concept")

    resolved = asyncio.run(probe())
    expected = {
        "insert": entity_id("Brand New", "Concept"),
        "exact": entity_id("Exact Resolve Fixture", "Author"),
        "fuzzy": entity_id("Existing", "Concept"),
        "path": None,
    }
    assert resolved == expected[scenario]
