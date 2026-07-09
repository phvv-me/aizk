import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import dbutil
import pytest
from doubles import RecordingEmbedder, RecordingReranker, deterministic_vector
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession
from strategies import community_notes, fact_hits, hits, raptor_notes, session_notes, short_text

from aizk.config import settings
from aizk.retrieval import (
    ContextPack,
    FactHit,
    Hit,
    LaneResult,
    RecallResult,
    SessionNote,
    graph_search,
    recall,
    search,
)
from aizk.retrieval.context import assemble_context_pack
from aizk.retrieval.lanes import fuse_lanes, route
from aizk.retrieval.query_route import QueryRoute
from aizk.retrieval.recall import (
    Recall,
    hybrid_recall_rows,
    latest_facts,
    session_hits,
    top_profile,
)
from aizk.serving import Embedder
from aizk.store import acting_as

DIM = settings.embed_dim
# a fixed past instant a claim's open recorded window starts at, so an as_of replay contains it.
PAST = datetime(2000, 1, 1, tzinfo=UTC)


def qvec(query: str) -> list[float]:
    """The vector the recording embedder produces for a query, so a seeded row can match it."""
    return deterministic_vector(f"query:{query}", DIM)


def other_vec(seed: str) -> list[float]:
    """A deterministic vector unrelated to any query, a far-but-nonzero match."""
    return deterministic_vector(f"other:{seed}", DIM)


def vec_sql(vector: list[float]) -> str:
    """Render a float vector as the pgvector text literal a halfvec CAST parses."""
    return "[" + ",".join(str(value) for value in vector) + "]"


async def seed_doc(
    owner: uuid.UUID,
    *,
    scopes: Sequence[uuid.UUID] = (),
    title: str | None = None,
    promoted_from: uuid.UUID | None = None,
) -> uuid.UUID:
    """Seed one document with control over its title and promotion provenance, return its id."""
    doc_id = uuid.uuid4()
    await dbutil.admin_exec(
        "INSERT INTO document (id, kind, content_hash, owner_id, scopes, title, promoted_from) "
        "VALUES (:id, 'note', 'seed', :owner, CAST(:scopes AS uuid[]), :title, :pf)",
        {
            "id": doc_id,
            "owner": owner,
            "scopes": [str(s) for s in scopes],
            "title": title,
            "pf": promoted_from,
        },
    )
    return doc_id


async def seed_chunk(
    doc_id: uuid.UUID,
    owner: uuid.UUID,
    body: str,
    vector: list[float],
    *,
    scopes: Sequence[uuid.UUID] = (),
    ord: int = 0,
) -> uuid.UUID:
    """Seed one embedded chunk under a document, return its id."""
    chunk_id = uuid.uuid4()
    await dbutil.admin_exec(
        "INSERT INTO chunk (id, document_id, ord, text, owner_id, scopes, embedding) "
        "VALUES (:id, :doc, :ord, :text, :owner, CAST(:scopes AS uuid[]), CAST(:emb AS halfvec))",
        {
            "id": chunk_id,
            "doc": doc_id,
            "ord": ord,
            "text": body,
            "owner": owner,
            "scopes": [str(s) for s in scopes],
            "emb": vec_sql(vector),
        },
    )
    return chunk_id


async def seed_entity(
    name: str,
    vector: list[float] | None = None,
    *,
    owner: uuid.UUID | None = None,
    scopes: Sequence[uuid.UUID] = (),
) -> uuid.UUID:
    """Seed one deduplicated entity content row, optionally embedded and claimed, return its id.

    owner: when given, also stake an entity claim so the content is visible under that user's
        row level security; entity content carries no owner of its own and is read only through a
        claim, so the portrait and raptor lanes that join it see nothing without one.
    """
    entity_id = uuid.uuid4()
    embedding = None if vector is None else vec_sql(vector)
    await dbutil.admin_exec(
        "INSERT INTO entity_content (id, name, type, embedding) "
        "VALUES (:id, :name, 'concept', CAST(:emb AS halfvec))",
        {"id": entity_id, "name": name, "emb": embedding},
    )
    if owner is not None:
        await dbutil.admin_exec(
            "INSERT INTO entity_claim (id, content_id, owner_id, scopes) "
            "VALUES (:id, :cid, :owner, CAST(:scopes AS uuid[]))",
            {
                "id": uuid.uuid4(),
                "cid": entity_id,
                "owner": owner,
                "scopes": [str(s) for s in scopes],
            },
        )
    return entity_id


async def seed_fact(
    owner: uuid.UUID,
    statement: str,
    vector: list[float],
    subject_id: uuid.UUID,
    *,
    object_id: uuid.UUID | None = None,
    scopes: Sequence[uuid.UUID] = (),
    recorded_from: datetime | None = None,
) -> uuid.UUID:
    """Seed one fact content plus a live claim on it, return the content id.

    recorded_from: lower bound of the open transaction-time window, so an as_of replay before now
        still contains it; the live `now()` default when null.
    """
    content_id = uuid.uuid4()
    await dbutil.admin_exec(
        "INSERT INTO fact_content (id, subject_id, object_id, predicate, statement, embedding) "
        "VALUES (:id, :subj, :obj, 'related_to', :stmt, CAST(:emb AS halfvec))",
        {
            "id": content_id,
            "subj": subject_id,
            "obj": object_id,
            "stmt": statement,
            "emb": vec_sql(vector),
        },
    )
    lower = "now()" if recorded_from is None else ":rec"
    await dbutil.admin_exec(
        "INSERT INTO fact_claim (id, content_id, owner_id, scopes, recorded) "
        f"VALUES (:id, :cid, :owner, CAST(:scopes AS uuid[]), tstzrange({lower}, NULL, '[)'))",
        {
            "id": uuid.uuid4(),
            "cid": content_id,
            "owner": owner,
            "scopes": [str(s) for s in scopes],
            "rec": recorded_from,
        },
    )
    return content_id


async def seed_profile(
    owner: uuid.UUID, subject_id: uuid.UUID, summary: str, *, scopes: Sequence[uuid.UUID] = ()
) -> None:
    """Seed one entity profile the portrait lane surfaces."""
    await dbutil.admin_exec(
        "INSERT INTO profile (id, owner_id, scopes, subject_id, summary) "
        "VALUES (:id, :owner, CAST(:scopes AS uuid[]), :subj, :summary)",
        {
            "id": uuid.uuid4(),
            "owner": owner,
            "scopes": [str(s) for s in scopes],
            "subj": subject_id,
            "summary": summary,
        },
    )


async def seed_community(
    owner: uuid.UUID,
    label: str,
    summary: str,
    vector: list[float],
    *,
    scopes: Sequence[uuid.UUID] = (),
) -> None:
    """Seed one community summary the thematic lane surfaces."""
    await dbutil.admin_exec(
        "INSERT INTO community (id, owner_id, scopes, label, summary, embedding, member_ids) "
        "VALUES (:id, :owner, CAST(:scopes AS uuid[]), :label, :summary, "
        "CAST(:emb AS halfvec), '{}')",
        {
            "id": uuid.uuid4(),
            "owner": owner,
            "scopes": [str(s) for s in scopes],
            "label": label,
            "summary": summary,
            "emb": vec_sql(vector),
        },
    )


async def seed_session(
    owner: uuid.UUID,
    body: str,
    vector: list[float],
    *,
    kind: str = "note",
    promoted: bool = False,
    scopes: Sequence[uuid.UUID] = (),
) -> None:
    """Seed one working-memory session item, promoted out of the working set when asked."""
    await dbutil.admin_exec(
        "INSERT INTO session_item (id, owner_id, scopes, kind, text, embedding, promoted_at) "
        "VALUES (:id, :owner, CAST(:scopes AS uuid[]), :kind, :text, CAST(:emb AS halfvec), "
        ":promoted)",
        {
            "id": uuid.uuid4(),
            "owner": owner,
            "scopes": [str(s) for s in scopes],
            "kind": kind,
            "text": body,
            "emb": vec_sql(vector),
            "promoted": datetime.now(UTC) if promoted else None,
        },
    )


async def in_session[T](
    user_id: uuid.UUID,
    body: Callable[[AsyncSession], Awaitable[T]],
    scopes: tuple[uuid.UUID, ...] = (),
) -> T:
    """Run one body on a user-scoped session, the shape the Recall unit tests share."""
    async with acting_as(user_id, scopes) as session:
        return await body(session)


async def access_count(content_id: uuid.UUID) -> int:
    """Read the live claim's access_count for a fact content, the record_access side effect."""
    async with dbutil.admin_engine().begin() as connection:
        return await connection.scalar(
            text("SELECT access_count FROM fact_claim WHERE content_id = :cid"),
            {"cid": content_id},
        )


def make_round(
    owner: uuid.UUID, query: str, k: int, *, ppr: bool, as_of: datetime | None = None
) -> Callable[[AsyncSession], Awaitable[Recall]]:
    """A session body building a Recall bound to a freshly embedded query, for the unit lanes."""

    async def body(session: AsyncSession) -> Recall:
        embedder = Embedder()
        [vector] = await embedder.embed([query], mode="query")
        return Recall(embedder, query, vector, k, as_of, ppr)

    return body


# ---- pure fusion and routing, no database -----------------------------------------------------


def lane_results() -> st.SearchStrategy[LaneResult]:
    """One lane's own slice, every field populated independently, the fuse input."""
    return st.builds(
        LaneResult,
        hits=st.lists(hits(), max_size=3),
        facts=st.lists(fact_hits(), max_size=3),
        session=st.lists(session_notes(), max_size=2),
        communities=st.lists(community_notes(), max_size=2),
        raptor=st.lists(raptor_notes(), max_size=2),
        profile=st.none() | short_text,
    )


@given(parts=st.lists(lane_results(), max_size=5))
def test_fuse_lanes_concatenates_each_field_in_order(parts: list[LaneResult]) -> None:
    """Fusing lays each lane's list end to end in order and takes the first non-empty profile."""
    fused = fuse_lanes(parts)
    assert fused.hits == [hit for part in parts for hit in part.hits]
    assert fused.facts == [fact for part in parts for fact in part.facts]
    assert fused.session == [note for part in parts for note in part.session]
    assert fused.communities == [note for part in parts for note in part.communities]
    assert fused.raptor == [note for part in parts for note in part.raptor]
    assert fused.profile == next((part.profile for part in parts if part.profile), None)


def test_fuse_lanes_of_nothing_is_the_empty_bundle() -> None:
    """Fusing no lanes yields the empty bundle, the base case a null recall degrades to."""
    assert fuse_lanes([]) == LaneResult()


@pytest.mark.parametrize("routing", [False, True])
def test_route_reads_the_router_only_when_routing_is_on(
    routing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing off falls back to the settings toggles, on it defers to the query router's plan."""
    query = "an overview of Alice and Bob"
    monkeypatch.setattr(settings, "query_routing", routing)
    thematic, ppr_on, raptor_on = route(query)
    if routing:
        plan = QueryRoute.plan(query)
        assert (thematic, ppr_on, raptor_on) == (plan.communities, plan.ppr, plan.raptor)
    else:
        assert thematic == QueryRoute.is_thematic(query)
        assert (ppr_on, raptor_on) == (settings.ppr, settings.raptor)


# ---- database-backed recall lanes -------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 2, 3, 5])
def test_hybrid_recall_recalls_the_matching_chunk_and_caps_at_k(
    k: int, migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """The closest chunk is always recalled and the chunk lane never returns more than k rows."""
    query = "leech lattice"

    async def flow() -> list[Row]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        doc = await seed_doc(owner, title="src")
        await seed_chunk(doc, owner, f"exact {query}", qvec(query), ord=0)
        await seed_chunk(doc, owner, "far one", other_vec("a"), ord=1)
        await seed_chunk(doc, owner, "far two", other_vec("b"), ord=2)
        return await in_session(owner, lambda s: hybrid_recall_rows(qvec(query), query, k))

    rows = dbutil.run(flow())
    chunk_texts = [row.text for row in rows if row.kind == "chunk"]
    assert f"exact {query}" in chunk_texts
    assert len(chunk_texts) == min(3, k)


def test_hybrid_recall_hides_another_owners_private_chunk(
    migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """A private chunk owned by a stranger never enters a user's fused pool."""
    query = "shared topic"

    async def flow() -> list[Row]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        stranger = uuid.uuid4()
        await dbutil.seed_user(owner)
        await dbutil.seed_user(stranger)
        mine = await seed_doc(owner, title="mine")
        theirs = await seed_doc(stranger, title="theirs")
        await seed_chunk(mine, owner, "my chunk", qvec(query))
        await seed_chunk(theirs, stranger, "secret chunk", qvec(query))
        return await in_session(owner, lambda s: hybrid_recall_rows(qvec(query), query, 8))

    rows = dbutil.run(flow())
    texts = [row.text for row in rows if row.kind == "chunk"]
    assert "my chunk" in texts
    assert "secret chunk" not in texts


def test_hybrid_recall_lens_narrows_to_the_named_group(
    migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """A scope lens keeps only the chunks shared into a named group, dropping the rest."""
    query = "grouped"

    async def flow() -> tuple[list[str], list[str]]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        group_a, group_b = uuid.uuid4(), uuid.uuid4()
        await dbutil.seed_group(group_a)
        await dbutil.seed_group(group_b)
        await dbutil.seed_membership(owner, group_a, "viewer")
        await dbutil.seed_membership(owner, group_b, "viewer")
        doc_a = await seed_doc(owner, title="a", scopes=[group_a])
        doc_b = await seed_doc(owner, title="b", scopes=[group_b])
        await seed_chunk(doc_a, owner, "in group a", qvec(query), scopes=[group_a])
        await seed_chunk(doc_b, owner, "in group b", qvec(query), scopes=[group_b])
        lensed = await in_session(
            owner, lambda s: hybrid_recall_rows(qvec(query), query, 8), scopes=(group_a,)
        )
        whole = await in_session(owner, lambda s: hybrid_recall_rows(qvec(query), query, 8))

        def chunk(rows: list[Row]) -> list[str]:
            return [r.text for r in rows if r.kind == "chunk"]

        return chunk(lensed), chunk(whole)

    lensed, whole = dbutil.run(flow())
    assert lensed == ["in group a"]
    assert set(whole) == {"in group a", "in group b"}


def test_hybrid_recall_promoted_bonus_lifts_a_promoted_chunk(
    migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """Two equidistant chunks split by the promoted bonus, the promoted document scoring higher."""
    query = "tie break"

    async def flow() -> dict[str, float]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        plain = await seed_doc(owner, title="plain")
        origin = await seed_doc(owner, title="origin")
        promoted = await seed_doc(owner, title="promoted", promoted_from=origin)
        await seed_chunk(plain, owner, "plain chunk", qvec(query))
        await seed_chunk(promoted, owner, "promoted chunk", qvec(query))
        rows = await in_session(owner, lambda s: hybrid_recall_rows(qvec(query), query, 8))
        return {row.text: row.score for row in rows if row.kind == "chunk"}

    scores = dbutil.run(flow())
    assert scores["promoted chunk"] > scores["plain chunk"]


@pytest.mark.parametrize("as_of", [None, datetime(2020, 6, 1, tzinfo=UTC)])
def test_latest_facts_ranks_visible_current_facts(
    as_of: datetime | None, migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """The closest live fact is ranked first, live or replayed at a past world-time."""
    query = "who wrote it"

    async def flow() -> list[FactHit]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        subject = await seed_entity("Alice")
        await seed_fact(owner, "alice wrote the paper", qvec(query), subject, recorded_from=PAST)
        await seed_fact(owner, "unrelated fact", other_vec("z"), subject, recorded_from=PAST)
        return await in_session(owner, lambda s: latest_facts(qvec(query), 5, as_of))

    facts = dbutil.run(flow())
    assert facts
    assert facts[0].statement == "alice wrote the paper"


def test_session_hits_only_ranks_unpromoted_items(
    migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """The working lane surfaces the still-working items and skips the promoted ones."""
    query = "recent capture"

    async def flow() -> list[SessionNote]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        await seed_session(owner, "still working", qvec(query), kind="note")
        await seed_session(owner, "already promoted", qvec(query), promoted=True)
        return await in_session(owner, lambda s: session_hits(qvec(query), 5))

    notes = dbutil.run(flow())
    texts = [note.text for note in notes]
    assert "still working" in texts
    assert "already promoted" not in texts


def test_top_profile_returns_the_closest_portrait_or_none(
    migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """The portrait lane returns the nearest profiled entity's summary, null when none profiled."""
    query = "about alice"

    async def flow() -> tuple[str | None, str | None]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        empty = await in_session(owner, lambda s: top_profile(qvec(query)))
        subject = await seed_entity("Alice", qvec(query), owner=owner)
        await seed_profile(owner, subject, "Alice is a mathematician.")
        filled = await in_session(owner, lambda s: top_profile(qvec(query)))
        return empty, filled

    empty, filled = dbutil.run(flow())
    assert empty is None
    assert filled == "Alice is a mathematician."


def test_graph_search_embeds_the_query_and_ranks_facts(
    migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """The thin graph entrypoint embeds once in query mode and returns the closest facts."""
    query = "graph entry"

    async def flow() -> list[FactHit]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        subject = await seed_entity("Node")
        await seed_fact(owner, "the ranked fact", qvec(query), subject)
        return await graph_search(query, k=5, user_id=owner)

    facts = dbutil.run(flow())
    assert [fact.statement for fact in facts] == ["the ranked fact"]
    assert ([query], "query") in fake_embedder.calls


@pytest.mark.parametrize("rerank", [True, False])
def test_search_returns_hits_reranked_only_when_enabled(
    rerank: bool,
    migrated_db: None,
    fake_embedder: RecordingEmbedder,
    fake_reranker: RecordingReranker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search fuses the chunk lane and reorders it with the reranker only when rerank is on."""
    query = "find the passage"
    monkeypatch.setattr(settings, "rerank", rerank)

    async def flow() -> list[Hit]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        doc = await seed_doc(owner, title="src")
        await seed_chunk(doc, owner, f"the passage for {query}", qvec(query))
        return await search(query, k=4, user_id=owner)

    hits_out = dbutil.run(flow())
    assert any("the passage" in hit.text for hit in hits_out)
    assert bool(fake_reranker.calls) is rerank


@pytest.mark.parametrize("query_routing", [False, True])
def test_recall_bundles_every_lane_over_the_seeded_graph(
    query_routing: bool,
    migrated_db: None,
    fake_embedder: RecordingEmbedder,
    fake_reranker: RecordingReranker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thematic recall fills the chunk, fact, session, community and profile slices at once.

    Seeds four matching chunks so the rerank pool clears its floor, a fact whose access the round
    records, a profiled subject, a community and a working item, then asserts every lane populated
    its own slice of the one bundle and record_access bumped the surfaced claim.
    """
    query = "overview of packings"
    monkeypatch.setattr(settings, "query_routing", query_routing)

    async def flow() -> tuple[RecallResult, int]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        doc = await seed_doc(owner, title="src")
        for index in range(4):
            await seed_chunk(doc, owner, f"packing chunk {index}", qvec(query), ord=index)
        subject = await seed_entity("Leech", qvec(query), owner=owner)
        fact = await seed_fact(owner, "the packing is optimal", qvec(query), subject)
        await seed_profile(owner, subject, "a rolled-up portrait")
        await seed_community(owner, "packings", "a cluster paragraph", qvec(query))
        await seed_session(owner, "a working note", qvec(query))
        result = await recall(query, user_id=owner, k=4)
        return result, await access_count(fact)

    result, count = dbutil.run(flow())
    assert isinstance(result, RecallResult)
    assert len(result.hits) == 4
    assert any("optimal" in fact.statement for fact in result.facts)
    assert result.communities
    assert result.session
    assert result.profile == "a rolled-up portrait"
    assert result.as_of is None
    assert count == 1


def test_recall_replays_the_graph_at_a_past_world_time(
    migrated_db: None, fake_embedder: RecordingEmbedder, fake_reranker: RecordingReranker
) -> None:
    """An as_of recall reads the seed and neighbor facts through the historical ORM path."""
    query = "as of history"
    as_of = datetime(2020, 6, 1, tzinfo=UTC)

    async def flow() -> RecallResult:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        alice = await seed_entity("Alice")
        bob = await seed_entity("Bob")
        await seed_fact(
            owner, "alice knew bob", qvec(query), alice, object_id=bob, recorded_from=PAST
        )
        await seed_fact(owner, "bob knew carol", other_vec("n"), bob, recorded_from=PAST)
        return await recall(query, user_id=owner, k=4, as_of=as_of)

    result = dbutil.run(flow())
    assert result.as_of == as_of
    assert any(fact.statement == "alice knew bob" for fact in result.facts)


@pytest.mark.parametrize("seed_evidence", [False, True])
def test_recall_fills_a_thin_recall_with_one_extra_round(
    seed_evidence: bool,
    migrated_db: None,
    fake_embedder: RecordingEmbedder,
    fake_reranker: RecordingReranker,
) -> None:
    """A recall under the hit floor issues one gap-fill round, re-embedding only when it has seeds.

    With no evidence the widened query is unchanged and the round reuses its own vector; with a
    single seed chunk and fact the query widens and the round re-embeds, both branches of fill_gap.
    """
    query = "thin recall"

    async def flow() -> RecallResult:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        if seed_evidence:
            doc = await seed_doc(owner, title="src")
            await seed_chunk(doc, owner, "lone chunk", qvec(query))
            subject = await seed_entity("Seed")
            await seed_fact(owner, "a lone fact", qvec(query), subject)
        return await recall(query, user_id=owner, k=4)

    result = dbutil.run(flow())
    assert isinstance(result, RecallResult)
    # a re-embed only happens when the thin round had a seed to widen the query with.
    query_embeds = [call for call in fake_embedder.calls if call[1] == "query"]
    assert (len(query_embeds) > 1) is seed_evidence


@pytest.mark.parametrize(
    ("field", "flag", "value"),
    [
        ("profile", "profiles", False),
        ("session", "session_recall_k", 0),
        ("communities", "query_routing", False),
        ("raptor", "raptor", False),
    ],
)
def test_recall_lane_toggles_leave_their_slice_empty(
    field: str,
    flag: str,
    value: bool | int,
    migrated_db: None,
    fake_embedder: RecordingEmbedder,
    fake_reranker: RecordingReranker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning a lane off leaves its slice empty while the rest of the bundle still assembles."""
    query = "Alice Bob Carol Dave"  # capitalized proper nouns keep the query pointed
    monkeypatch.setattr(settings, flag, value)

    async def flow() -> RecallResult:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        subject = await seed_entity("Alice", qvec(query), owner=owner)
        await seed_profile(owner, subject, "a portrait")
        await seed_community(owner, "c", "a cluster", qvec(query))
        await seed_session(owner, "a note", qvec(query))
        return await recall(query, user_id=owner, k=4)

    result = dbutil.run(flow())
    assert not getattr(result, field)


def test_neighbor_facts_widens_from_the_closest_seed(
    migrated_db: None, fake_embedder: RecordingEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-hop neighbors of the closest seed fact surface, the seed fact itself excluded."""
    query = "neighbor walk"
    monkeypatch.setattr(settings, "graph_facts_k", 1)

    async def flow() -> list[FactHit]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        alice = await seed_entity("Alice")
        bob = await seed_entity("Bob")
        await seed_fact(owner, "closest seed", qvec(query), alice, object_id=bob)
        await seed_fact(owner, "adjacent neighbor", other_vec("n"), alice)

        async def run(session: AsyncSession) -> list[FactHit]:
            round_ = await make_round(owner, query, 5, ppr=False)(session)
            return await round_.neighbor_facts()

        return await in_session(owner, run)

    facts = dbutil.run(flow())
    statements = [fact.statement for fact in facts]
    assert "adjacent neighbor" in statements
    assert "closest seed" not in statements


def test_ppr_facts_reaches_the_multi_hop_neighborhood(
    migrated_db: None, fake_embedder: RecordingEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The multi-hop lane reaches a fact past the one-hop seed through a pagerank walk."""
    query = "hippo walk"
    monkeypatch.setattr(settings, "graph_facts_k", 1)
    monkeypatch.setattr(settings, "ppr_margin", 0.0)

    async def flow() -> list[FactHit]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        alice = await seed_entity("Alice")
        bob = await seed_entity("Bob")
        carol = await seed_entity("Carol")
        await seed_fact(owner, "alice to bob", qvec(query), alice, object_id=bob)
        await seed_fact(owner, "bob to carol", qvec(query), bob, object_id=carol)

        async def run(session: AsyncSession) -> list[FactHit]:
            round_ = await make_round(owner, query, 5, ppr=True)(session)
            return await round_.ppr_facts()

        return await in_session(owner, run)

    facts = dbutil.run(flow())
    assert any(fact.statement == "bob to carol" for fact in facts)


def test_recall_round_wrappers_and_seedless_lanes(
    migrated_db: None, fake_embedder: RecordingEmbedder
) -> None:
    """The round's portrait and working wrappers read its bound vector; the neighbor and multihop
    lanes are empty when the graph holds no fact to seed the walk and the ppr toggle is off.
    """
    query = "round wrappers"

    async def flow() -> tuple[str | None, list[SessionNote], list[FactHit], list[FactHit]]:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        subject = await seed_entity("Subject", qvec(query), owner=owner)
        await seed_profile(owner, subject, "the bound portrait")
        await seed_session(owner, "a working item", qvec(query))

        async def run(
            session: AsyncSession,
        ) -> tuple[str | None, list[SessionNote], list[FactHit], list[FactHit]]:
            round_ = await make_round(owner, query, 5, ppr=False)(session)
            portrait = await round_.top_profile()
            notes = await round_.session_hits(5)
            neighbors = await round_.neighbor_facts()
            multihop = await round_.multihop_facts()
            return portrait, notes, neighbors, multihop

        return await in_session(owner, run)

    profile, notes, neighbors, multihop = dbutil.run(flow())
    assert profile == "the bound portrait"
    assert [note.text for note in notes] == ["a working item"]
    assert neighbors == []
    assert multihop == []


def test_assemble_context_pack_recalls_and_packs_the_seeded_graph(
    migrated_db: None, fake_embedder: RecordingEmbedder, fake_reranker: RecordingReranker
) -> None:
    """The pack entrypoint runs a real recall and renders the seeded chunk into a sources block."""
    query = "pack me"

    async def flow() -> ContextPack:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await dbutil.seed_user(owner)
        doc = await seed_doc(owner, title="src")
        await seed_chunk(doc, owner, f"packable passage for {query}", qvec(query))
        return await assemble_context_pack(query, user_id=owner, token_budget=4000, k=4)

    pack = dbutil.run(flow())
    assert isinstance(pack, ContextPack)
    assert pack.query == query
    assert pack.used_tokens <= pack.budget
    source_lines = [block.line for block in pack.blocks if block.lane == "sources"]
    assert any("packable passage" in line for line in source_lines)
