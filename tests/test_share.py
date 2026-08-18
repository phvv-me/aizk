import asyncio
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from importlib import import_module
from math import ceil
from typing import cast

import dbutil
import pytest
from bg_doubles import fake_artifact_services
from doubles import deterministic_vector
from id_factory import uuid5, uuid7, uuid8
from pydantic import UUID5, UUID7
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from aizk.config import settings
from aizk.exceptions import ScopeNotFoundError
from aizk.memory import Memory, SharedDocument, ShareResult
from aizk.retrieval import Candidate, Lane
from aizk.retrieval import documents as selected_documents
from aizk.retrieval.packing import pack
from aizk.store import Chunk, Document, Entity, Fact
from aizk.store.identity import OrganizationStanding, User
from aizk.store.locking import document_revision

pytestmark = pytest.mark.usefixtures("migrated_db")

# The graph package re-exports the `promote` verb, which shadows the submodule of the same name.
promote_module = import_module("aizk.graph.promote")
ingest_module = import_module("aizk.extract.ingest")

QUESTION = "what does interpretability need"


@pytest.fixture
def owner() -> Iterator[UUID5]:
    dbutil.run(dbutil.reset_db())
    yield uuid5()
    dbutil.run(dbutil.reset_db())


def team_caller(owner: UUID5, name: str = "Team") -> tuple[User, UUID5]:
    """One caller who owns a private scope and may write into one organization."""
    team = settings.scope_id(f"{name}-{uuid5()}")
    return (
        User.authorized(
            owner,
            read=(owner, team),
            write=(owner, team),
            organizations=(OrganizationStanding(id=team, name=name),),
        ),
        team,
    )


def memory(user: User) -> Memory:
    """The memory service for one caller over inert artifact intake."""
    return Memory(user, fake_artifact_services().intake)


async def share_topic(user: User, scopes: list[str], limit: int = 20) -> ShareResult:
    """Run the two steps a caller runs: preview a topic, then act on the ids it offered."""
    offered = await memory(user).share(query=QUESTION, scopes=scopes, limit=limit)
    return await memory(user).share([item.id for item in offered.documents], scopes=scopes)


async def seed_note(user: User, title: str, text: str, scopes: list[UUID5]) -> UUID7:
    """One document whose single chunk sits exactly on the question's query vector."""
    document = Document(
        id=uuid7(),
        content_hash=uuid8(),
        created_by=user.id,
        scopes=scopes,
        title=title,
    )
    async with user as session:
        session.add(document)
        await session.flush()
        session.add(
            Chunk(
                document_id=document.id,
                ord=0,
                text=text,
                embedding=deterministic_vector(f"query:{QUESTION}", settings.embed_dim),
                created_by=user.id,
                scopes=scopes,
            )
        )
    return document.id


async def seed_grounded_fact(user: User, document_id: UUID7, statement: str) -> UUID7:
    """One live claim sourced from a document's own chunk, the graph a transfer carries."""
    entity = Entity.Content(id=uuid5(), name=f"entity {uuid7()}", type="concept")
    content = Fact.Content(
        id=uuid5(),
        subject_id=entity.id,
        predicate="related_to",
        statement=statement,
    )
    async with User.system().owner as session:
        chunk = (
            await session.exec(select(Chunk.id).where(Chunk.document_id == document_id))
        ).one()
        session.add(entity)
        await session.flush()
        session.add(Entity.Claim(content_id=entity.id, created_by=user.id, scopes=[user.id]))
        session.add(content)
        await session.flush()
        claim = Fact.Claim(
            content_id=content.id,
            created_by=user.id,
            scopes=[user.id],
            source_chunk_id=chunk,
        )
        session.add(claim)
    return claim.id


async def live_claims(document_id: UUID7) -> int:
    """How many claims a document's chunks still ground, the retraction's observable."""
    async with User.system().owner as session:
        return (
            await session.exec(
                select(Fact.Live.id.count()).where(
                    Fact.Live.source_chunk_id.in_(
                        select(Chunk.id).where(Chunk.document_id == document_id)
                    )
                )
            )
        ).one()


async def copies_of(source: UUID7, scopes: list[UUID5]) -> list[UUID7]:
    """Every promoted copy of one source standing in an exact scope set."""
    async with User.system().owner as session:
        return list(
            await session.exec(
                select(Document.id).where(
                    Document.promoted_from == source, Document.scopes == sorted(scopes)
                )
            )
        )


async def revise(user: User, document_id: UUID7, text: str) -> None:
    """Re-keep one note: close the claims its old span grounded, then rewrite the span.

    Ingestion retracts a source's live claims before its chunks change, so the revision
    mirrors that order rather than leaving the previous statements live beside the new ones.
    """
    async with user as session:
        await Fact.Claim.retract_from_documents(session, [document_id], "superseded")
        chunk = (await session.exec(select(Chunk).where(Chunk.document_id == document_id))).one()
        chunk.text = text
        source = (await session.exec(select(Document).where(Document.id == document_id))).one()
        source.content_hash = uuid8()
        session.add_all((chunk, source))


async def spans_of(document_id: UUID7) -> list[str]:
    """The text a document's chunks currently hold, in document order."""
    async with User.system().owner as session:
        return list(
            await session.exec(
                select(Chunk.text).where(Chunk.document_id == document_id).order_by(Chunk.ord)
            )
        )


async def live_statements(document_id: UUID7) -> list[str]:
    """The statements a document's chunks still ground live, the graph a copy carries."""
    async with User.system().owner as session:
        return list(
            await session.exec(
                select(Fact.Live.statement).where(
                    Fact.Live.source_chunk_id.in_(
                        select(Chunk.id).where(Chunk.document_id == document_id)
                    )
                )
            )
        )


@pytest.mark.parametrize(
    ("documents", "query"),
    [(None, None), ([uuid7()], "a question")],
    ids=["neither", "both"],
)
def test_share_demands_exactly_one_selection(
    owner: UUID5, documents: list[UUID7] | None, query: str | None
) -> None:
    user, _ = team_caller(owner)

    with pytest.raises(ValueError, match="either explicit documents or one selection query"):
        dbutil.run(memory(user).share(documents, query=query))


@pytest.mark.parametrize("move", [False, True], ids=["copy", "move"])
def test_share_names_the_guard_that_refused_without_saying_whether_it_exists(
    owner: UUID5, move: bool
) -> None:
    user, team = team_caller(owner)
    # one stranger's document and one id that never existed must read exactly alike
    stranger = dbutil.run(dbutil.seed_document(uuid5(), [uuid5()]))
    refused = "among your own private documents" if move else "visible to you"

    for named in (stranger, uuid7()):
        with pytest.raises(ValueError, match=refused):
            dbutil.run(memory(user).share([named], scopes=["Team"], move=move))
    assert dbutil.run(copies_of(stranger, [team])) == []


@pytest.mark.parametrize(
    ("documents", "query"),
    [(None, QUESTION), ([uuid7()], None)],
    ids=["query", "move"],
)
def test_a_personal_selection_refuses_to_carry_a_scope_onto_itself(
    owner: UUID5, documents: list[UUID7] | None, query: str | None
) -> None:
    user, _ = team_caller(owner)

    with pytest.raises(ValueError, match="need an organization destination"):
        dbutil.run(memory(user).share(documents, query=query, move=documents is not None))


def test_share_leaves_a_document_already_standing_in_the_destination_alone(owner: UUID5) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[ShareResult, list[UUID7]]:
        standing = await seed_note(user, "Team brief", "probes and features", [team])
        return await memory(user).share([standing], scopes=["Team"]), await copies_of(
            standing, [team]
        )

    result, copies = dbutil.run(probe())

    assert result == ShareResult()  # the end state the caller asked for already held
    assert copies == []  # no generation was bred beside it


def test_share_refuses_a_destination_outside_the_callers_write_authority(owner: UUID5) -> None:
    user, _ = team_caller(owner)

    with pytest.raises(ScopeNotFoundError, match="no writable scope named 'Unknown'"):
        dbutil.run(memory(user).share([uuid7()], scopes=["Unknown"]))


@pytest.mark.parametrize("dry_run", [False, True], ids=["asking-to-act", "asking-to-look"])
def test_a_query_only_ever_previews_whatever_else_is_asked_of_it(
    owner: UUID5, dry_run: bool
) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[ShareResult, list[UUID7]]:
        private = await seed_note(user, "Interpretability", "probes and features", [owner])
        # a document already standing in the organization is not the caller's to select
        shared = await seed_note(user, "Team brief", "probes and features", [team])
        offered = await memory(user).share(query=QUESTION, scopes=["Team"], preview=dry_run)
        return offered, [private, shared]

    offered, (private, shared) = dbutil.run(probe())

    # a query answers what it would take and stops, so the result is marked and carries no copy
    assert offered == ShareResult(
        documents=(SharedDocument(id=private, title="Interpretability"),),
        preview=True,
    )
    assert dbutil.run(copies_of(private, [team])) == []
    assert shared not in {item.id for item in offered.documents}


def test_a_query_refuses_to_move_rather_than_quietly_declining_to(owner: UUID5) -> None:
    user, team = team_caller(owner)

    async def probe() -> list[UUID7]:
        source = await seed_note(user, "Interpretability", "probes and features", [owner])
        with pytest.raises(ValueError, match="only ever previews, so it cannot move"):
            await memory(user).share(query=QUESTION, scopes=["Team"], move=True)
        return await copies_of(source, [team])

    # refusing is the point: a silently ignored move reads as a move that happened
    assert dbutil.run(probe()) == []


@pytest.mark.parametrize("move", [False, True], ids=["copy", "move"])
def test_a_dry_run_previews_an_explicit_list_without_writing(owner: UUID5, move: bool) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[ShareResult, list[UUID7]]:
        source = await seed_note(user, "Interpretability", "probes and features", [owner])
        offered = await memory(user).share([source], scopes=["Team"], move=move, preview=True)
        return offered, await copies_of(source, [team])

    offered, copies = dbutil.run(probe())

    # previewing an exact list is a real question, so it answers what it would do and writes none
    assert offered.preview and offered.moved is move
    assert [item.title for item in offered.documents] == ["Interpretability"]
    assert all(item.destination is None for item in offered.documents)
    assert copies == []


def test_the_two_step_flow_previews_a_topic_then_writes_the_approved_ids(owner: UUID5) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[ShareResult, ShareResult, list[UUID7]]:
        source = await seed_note(user, "Interpretability", "probes and features", [owner])
        offered = await memory(user).share(query=QUESTION, scopes=["Team"])
        acted = await memory(user).share([item.id for item in offered.documents], scopes=["Team"])
        return offered, acted, await copies_of(source, [team])

    offered, acted, copies = dbutil.run(probe())

    assert offered.preview and all(item.destination is None for item in offered.documents)
    assert not acted.preview
    (carried,) = acted.documents
    assert carried.destination is not None and copies == [carried.destination]


def test_move_copies_into_the_organization_and_retires_the_private_original(
    owner: UUID5,
) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[ShareResult, ShareResult, int, str]:
        source = await seed_note(user, "Interpretability", "probes and features", [owner])
        await seed_grounded_fact(user, source, "probing needs features")
        moved = await memory(user).share([source], scopes=["Team"], move=True)
        # repeating the same move finds the standing copy and the expired original
        repeated = await memory(user).share([source], scopes=["Team"], move=True)
        remaining = await live_claims(source)
        found = await (await memory(user).find_memory(QUESTION, budget=2048)).to_markdown()
        return moved, repeated, remaining, found

    moved, repeated, remaining, found = dbutil.run(probe())

    (carried,) = moved.documents
    assert moved.moved and not moved.preview
    assert carried.title == "Interpretability"
    assert carried.destination is not None
    assert dbutil.run(copies_of(carried.id, [team])) == [carried.destination]
    # the repeat settles on the copy it already made rather than making a second one
    assert repeated == moved
    assert remaining == 0  # the source's claims are closed, not deleted
    # the evidence survives the move, but find now reaches it only through the organization
    assert str(carried.id) not in found
    assert f"Document `{carried.destination}" in found
    assert "from scope `Team`" in found


def test_copy_leaves_the_private_original_standing_in_find(owner: UUID5) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[ShareResult, str]:
        source = await seed_note(user, "Interpretability", "probes and features", [owner])
        copied = await memory(user).share([source], scopes=["Team"])
        found = await (await memory(user).find_memory(QUESTION, budget=2048)).to_markdown()
        return copied, found

    copied, found = dbutil.run(probe())

    (carried,) = copied.documents
    assert not copied.moved
    assert dbutil.run(copies_of(carried.id, [team])) == [carried.destination]
    assert "probes and features" in found


def test_query_selection_returns_distinct_documents_in_merit_order(owner: UUID5) -> None:
    user = User.private(owner)

    async def probe() -> tuple[set[UUID7], list[UUID7], list[UUID7]]:
        seeded = {
            await seed_note(user, "First", "probes and features", [owner]),
            await seed_note(user, "Second", "features and probes", [owner]),
        }
        return (
            seeded,
            await selected_documents(QUESTION, user, limit=5),
            await selected_documents(QUESTION, user, limit=1),
        )

    seeded, both, capped = dbutil.run(probe())

    assert len(both) == len(set(both)) == 2  # several chunks of one document name it once
    assert set(both) == seeded
    assert len(capped) == 1 and capped[0] in seeded


def test_a_query_matching_nothing_still_answers_as_a_preview(owner: UUID5) -> None:
    user, team = team_caller(owner)

    result = dbutil.run(memory(user).share(query=QUESTION, scopes=["Team"]))

    assert result == ShareResult(preview=True)
    assert dbutil.run(copies_of(uuid7(), [team])) == []


def test_a_source_revised_after_a_share_refreshes_its_copy_before_a_move_retires_it(
    owner: UUID5,
) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[ShareResult, list[str], list[str], list[UUID7]]:
        source = await seed_note(user, "Interpretability", "the first draft", [owner])
        await seed_grounded_fact(user, source, "the first claim")
        await memory(user).share([source], scopes=["Team"])
        # the note is re-kept with new content, then moved into the same organization
        await revise(user, source, "the revised draft")
        await seed_grounded_fact(user, source, "the revised claim")
        moved = await memory(user).share([source], scopes=["Team"], move=True)
        (carried,) = moved.documents
        destination = carried.destination
        assert destination is not None
        return (
            moved,
            await spans_of(destination),
            await live_statements(destination),
            await copies_of(source, [team]),
        )

    moved, spans, statements, copies = dbutil.run(probe())

    (carried,) = moved.documents
    assert copies == [carried.destination]  # refreshed in place, never a second generation
    assert spans == ["the revised draft"]  # the move cannot leave the stale text behind
    assert "the revised claim" in statements
    assert "the first claim" not in statements  # the stale claim closed rather than lingering


def test_a_copy_retired_on_its_own_is_refreshed_rather_than_left_dead(owner: UUID5) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[list[str], bool]:
        source = await seed_note(user, "Interpretability", "the only draft", [owner])
        first = await memory(user).share([source], scopes=["Team"])
        (carried,) = first.documents
        destination = carried.destination
        assert destination is not None
        # something retires the destination while the source still holds
        async with User.system((team,)) as session:
            await Document.retire(session, [destination])
        await memory(user).share([source], scopes=["Team"], move=True)
        async with User.system().owner as session:
            copy = (await session.exec(select(Document).where(Document.id == destination))).one()
        return await spans_of(destination), copy.active_at(datetime.now(UTC))

    spans, destination_active = dbutil.run(probe())

    assert spans == ["the only draft"]
    # a move that retires the source must leave a live destination behind, never nothing
    assert destination_active


def test_a_repeated_move_never_stamps_the_sources_tombstone_onto_the_copy(owner: UUID5) -> None:
    user, _ = team_caller(owner)

    async def probe() -> tuple[bool, bool, ShareResult]:
        source = await seed_note(user, "Interpretability", "the only draft", [owner])
        first = await memory(user).share([source], scopes=["Team"], move=True)
        (carried,) = first.documents
        repeated = await memory(user).share([source], scopes=["Team"], move=True)
        async with User.system().owner as session:
            rows = {
                document.id: document
                for document in await session.exec(
                    select(Document).where(Document.id.in_([source, carried.destination]))
                )
            }
        now = datetime.now(UTC)
        return (
            rows[source].active_at(now),
            rows[cast("UUID7", carried.destination)].active_at(now),
            repeated,
        )

    source_active, destination_active, repeated = dbutil.run(probe())

    assert not source_active  # the original stays retired
    assert destination_active  # and the repeat leaves the destination live
    assert repeated.documents  # the repeat still reports the copy it settled on


def test_a_move_that_fails_midway_commits_neither_the_copy_nor_the_retirement(
    owner: UUID5, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, team = team_caller(owner)

    async def refuse(cls: object, session: object, document_ids: object) -> list[UUID7]:
        raise RuntimeError("the retirement half died")

    async def probe() -> tuple[UUID7, list[UUID7], bool]:
        source = await seed_note(user, "Interpretability", "the only draft", [owner])
        monkeypatch.setattr(promote_module.Document, "retire", classmethod(refuse))
        with pytest.raises(RuntimeError, match="the retirement half died"):
            await memory(user).share([source], scopes=["Team"], move=True)
        monkeypatch.undo()
        async with User.system().owner as session:
            standing = (await session.exec(select(Document).where(Document.id == source))).one()
        return source, await copies_of(source, [team]), standing.active_at(datetime.now(UTC))

    source, copies, source_active = dbutil.run(probe())

    # one transaction carries both halves, so a dead retirement rolls the copy back with it
    assert copies == []
    assert source_active
    # and the move runs cleanly afterwards because nothing was half done
    assert dbutil.run(memory(team_caller(owner)[0]).share([source], scopes=["Team"])).documents


def test_concurrent_shares_of_one_source_settle_on_a_single_destination(owner: UUID5) -> None:
    first_user, team = team_caller(owner)
    second_user = User.authorized(
        owner,
        read=(owner, team),
        write=(owner, team),
        organizations=(OrganizationStanding(id=team, name="Team"),),
    )

    async def probe() -> tuple[list[UUID7], list[BaseException | ShareResult]]:
        source = await seed_note(first_user, "Interpretability", "the only draft", [owner])
        racing = await asyncio.gather(
            memory(first_user).share([source], scopes=["Team"]),
            memory(second_user).share([source], scopes=["Team"]),
            return_exceptions=True,
        )
        return await copies_of(source, [team]), list(racing)

    copies, racing = dbutil.run(probe())

    # the transaction lock serializes the pair and the unique index is the durable backstop
    assert len(copies) == 1
    assert any(isinstance(outcome, ShareResult) for outcome in racing)


def test_a_query_selection_keeps_the_private_originals_the_organization_copies_outrank(
    owner: UUID5,
) -> None:
    user, _ = team_caller(owner)

    async def probe() -> tuple[set[UUID7], list[UUID7]]:
        seeded = {
            await seed_note(user, f"Interpretability {index}", "probes and features", [owner])
            for index in range(3)
        }
        # an earlier share leaves organization copies that carry the promoted bonus
        await share_topic(user, ["Team"], limit=3)
        return seeded, await selected_documents(QUESTION, user, limit=3, scopes=frozenset({owner}))

    seeded, reselected = dbutil.run(probe())

    # the boosted copies must not spend the caller's limit and displace their own originals
    assert set(reselected) == seeded


def test_packing_prices_the_document_line_it_will_render(owner: UUID5) -> None:
    del owner
    plain = Candidate(lane=Lane.Kind.SOURCES, line="an evidence line")
    annotated = plain.model_copy(
        update={"document_id": uuid7(), "document_created_at": datetime.now(UTC)}
    )

    assert annotated.document_note is not None
    assert annotated.token_count > plain.token_count
    # the annotation is priced at what it renders, not ignored until it overruns the budget
    assert annotated.token_count == ceil(
        (len(plain.line) + len(annotated.document_note) + len("\n\n    Document ``"))
        / settings.find_chars_per_token
    )
    # a budget that holds the bare line does not hold the annotated one, so packing skips it
    assert pack([plain, annotated], plain.token_count + 1) == [plain]


def test_the_database_refuses_a_second_copy_of_one_source_per_destination(owner: UUID5) -> None:
    user, team = team_caller(owner)

    async def probe() -> None:
        source = await seed_note(user, "Interpretability", "the only draft", [owner])
        (carried,) = (await memory(user).share([source], scopes=["Team"])).documents
        assert carried.destination is not None
        # the advisory lock serializes racing shares, and this index is what makes the rule true
        async with User.system((team,)) as session:
            session.add(
                Document(
                    id=uuid7(),
                    content_hash=uuid8(),
                    created_by=owner,
                    scopes=[team],
                    title="a second generation",
                    promoted_from=source,
                )
            )

    with pytest.raises(IntegrityError, match="uq_document_promotion_scope"):
        dbutil.run(probe())


def quadrant_pair(hashes_match: bool, copy_live: bool, source_live: bool) -> tuple[Document, ...]:
    """One source and its standing copy posed at an exact hash and activity quadrant."""
    past, digest = datetime(2020, 1, 1, tzinfo=UTC), uuid8()
    return tuple(
        Document(
            id=uuid7(),
            content_hash=digest if shared_hash else uuid8(),
            expires_at=None if live else past,
            created_by=uuid5(),
            scopes=[uuid5()],
        )
        for shared_hash, live in ((True, copy_live), (hashes_match, source_live))
    )


@pytest.mark.parametrize("source_live", [True, False], ids=["source-live", "source-retired"])
@pytest.mark.parametrize("copy_live", [True, False], ids=["copy-live", "copy-retired"])
@pytest.mark.parametrize("hashes_match", [True, False], ids=["same-hash", "changed-hash"])
def test_every_hash_and_activity_quadrant_keeps_a_reachable_destination(
    owner: UUID5, hashes_match: bool, copy_live: bool, source_live: bool
) -> None:
    del owner
    copy, source = quadrant_pair(hashes_match, copy_live, source_live)
    now = datetime.now(UTC)

    reused = promote_module.Promoter.stands_for(copy, source, now)
    expiry = promote_module.Promoter.inherited_expiry(copy, source, now)

    # A copy is left alone exactly when it already carries the content and needs no revival.
    assert reused == (hashes_match and (copy_live or not source_live))
    if reused:
        return
    refreshed = copy.model_copy(update={"expires_at": expiry})
    # Whatever the quadrant, a refresh may never be the step that retires a live copy, and
    # it must revive a copy its still-standing source outlived.
    assert refreshed.active_at(now) == (copy_live or source_live)


def test_ingestion_and_sharing_queue_on_one_lock_per_document(owner: UUID5) -> None:
    user, team = team_caller(owner)
    taken: list[list[str]] = []

    async def record(session: object, keys: Iterable[str]) -> None:
        del session
        taken.append(sorted(keys))

    async def probe(monkeypatch: pytest.MonkeyPatch) -> UUID7:
        source = await seed_note(user, "Interpretability", "the first draft", [owner])
        revised = Document(
            id=uuid7(),
            content_hash=uuid8(),
            created_by=owner,
            scopes=[owner],
            title="Interpretability",
        )
        monkeypatch.setattr(ingest_module, "acquire_locks", record)
        monkeypatch.setattr(promote_module, "acquire_locks", record)
        async with user as session:
            await ingest_module.DocumentStore(session).store(Document.id == source, revised)
            await promote_module.Promoter(session, sorted({team}), user.id).carry([source])
        return source

    with pytest.MonkeyPatch.context() as monkeypatch:
        source = dbutil.run(probe(monkeypatch))

    # revising a document and sharing it both queue on the one key that names the document
    assert taken[0] == [document_revision(source)]
    assert taken[1] == [document_revision(source)]


def test_a_batch_claims_its_originals_in_one_sorted_call(owner: UUID5) -> None:
    user, team = team_caller(owner)
    first, second = sorted((uuid7(), uuid7()))
    sources = [
        Document(
            id=uuid7(),
            content_hash=uuid8(),
            artifact_id=artifact,
            created_by=owner,
            scopes=[owner],
        )
        for artifact in (second, first)
    ]

    keys = promote_module.Promoter.artifact_keys(sources)

    # named in caller order here, but acquired sorted, so two batches cannot invert on them
    assert sorted(keys) == [f"artifact|{first}", f"artifact|{second}"]
    assert promote_module.Promoter.artifact_keys([]) == []
    del user, team


def test_naming_one_document_twice_carries_and_counts_it_once(owner: UUID5) -> None:
    user, team = team_caller(owner)

    async def probe() -> tuple[ShareResult, list[UUID7]]:
        source = await seed_note(user, "Interpretability", "the only draft", [owner])
        shared = await memory(user).share([source, source, source], scopes=["Team"])
        return shared, await copies_of(source, [team])

    shared, copies = dbutil.run(probe())

    assert len(shared.documents) == 1
    assert len(copies) == 1


def test_an_owned_selection_only_ever_names_the_callers_own_documents(owner: UUID5) -> None:
    user, _ = team_caller(owner)

    async def probe() -> tuple[set[UUID7], list[UUID7], list[UUID7]]:
        private = {
            await seed_note(user, f"Interpretability {index}", "probes and features", [owner])
            for index in range(4)
        }
        # every one of them now has an organization copy carrying the promoted bonus
        await share_topic(user, ["Team"], limit=4)
        return (
            private,
            await selected_documents(QUESTION, user, limit=8),
            await selected_documents(QUESTION, user, limit=4, scopes=frozenset({owner})),
        )

    private, unrestricted, restricted = dbutil.run(probe())

    # the corpus really does hold copies the caller could never carry
    assert any(item not in private for item in unrestricted)
    # yet the restricted cut fills its whole limit from private documents alone, because the
    # lane never ranked the copies rather than ranking them and dropping them after the cut
    assert len(restricted) == 4
    assert set(restricted) == private
