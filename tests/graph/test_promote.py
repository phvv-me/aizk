from unittest.mock import AsyncMock, MagicMock

import dbutil
import pytest
from id_factory import uuid5, uuid7, uuid8
from pydantic import UUID5, UUID7
from sqlalchemy import text
from sqlmodel import select

from aizk.config import settings
from aizk.exceptions import NotVisibleError
from aizk.graph.promote import promote
from aizk.store import (
    Artifact,
    Blob,
    Chunk,
    Document,
    Entity,
    Fact,
)
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")

UNIT_VECTOR = [1.0] + [0.0] * 1023


async def seed_source(promoter: UUID5 | UUID7) -> UUID5 | UUID7:
    document, chunk, entity = uuid7(), uuid7(), uuid5()
    async with dbutil.actor(promoter) as session:
        session.add(
            Document(
                id=document,
                content_hash=uuid8(),
                created_by=promoter,
                scopes=[promoter],
                title="source",
            )
        )
        session.add(
            Chunk(
                id=chunk,
                document_id=document,
                ord=0,
                text="span",
                created_by=promoter,
                scopes=[promoter],
            )
        )
        session.add(Entity.Content(id=entity, name="Leech", type="concept", embedding=UNIT_VECTOR))
        await session.flush()
        session.add(Entity.Claim(content_id=entity, created_by=promoter, scopes=[promoter]))
        content = Fact.Content(
            id=uuid5(),
            subject_id=entity,
            predicate="related_to",
            statement="the source fact",
            embedding=UNIT_VECTOR,
        )
        session.add(content)
        await session.flush()
        session.add(
            Fact.Claim(
                content_id=content.id,
                created_by=promoter,
                scopes=[promoter],
                source_chunk_id=chunk,
            )
        )
    return document


async def visible_copy(
    reader: UUID5 | UUID7, source: UUID5 | UUID7, orgs: tuple[UUID5 | UUID7, ...] = ()
) -> UUID5 | UUID7 | None:
    user = User.authorized(reader, read=(reader, *orgs))
    async with user as session:
        return (
            await session.exec(
                text("SELECT id FROM document WHERE promoted_from = :src"),
                params={"src": source},
            )
        ).scalar_one_or_none()


def test_promote_copies_once_into_scope_and_an_outsider_stays_blind() -> None:
    async def probe() -> tuple[
        int, int, UUID5 | UUID7 | None, UUID5 | UUID7 | None, UUID5 | UUID7 | None, list
    ]:
        await dbutil.reset_db()
        promoter, member, outsider = uuid5(), uuid5(), uuid5()
        team_org = f"team-{uuid5()}"
        team_scope = settings.scope_id(team_org)
        source = await seed_source(promoter)
        user = User.authorized(
            promoter,
            read=(promoter, team_scope),
            write=(promoter, team_scope),
        )
        count = await promote([source], frozenset({team_scope}), user)
        repeated = await promote([source], frozenset({team_scope}), user)
        async with dbutil.actor(promoter) as session:
            source_scopes = (
                await session.exec(
                    text("SELECT scopes FROM document WHERE id = :id"), params={"id": source}
                )
            ).scalar_one()
        return (
            count,
            repeated,
            await visible_copy(promoter, source, (team_scope,)),
            await visible_copy(member, source, (team_scope,)),
            await visible_copy(outsider, source),
            list(source_scopes),
        )

    count, repeated, promoter_sees, member_sees, outsider_sees, source_scopes = dbutil.run(probe())
    assert count == 1 and repeated == 0
    assert promoter_sees is not None
    assert member_sees == promoter_sees  # a member standing in the target org reads the same copy
    assert outsider_sees is None  # no standing in the target org, no copy
    assert len(source_scopes) == 1


def test_promote_of_an_invisible_document_raises() -> None:
    async def probe() -> None:
        await dbutil.reset_db()
        promoter = uuid5()
        with pytest.raises(NotVisibleError, match="no visible document"):
            await promote([uuid5()], frozenset({promoter}), User.private(promoter))

    dbutil.run(probe())


def test_sharing_rejects_a_document_with_broken_artifact_links() -> None:
    result = MagicMock()
    result.first.return_value = None
    session = AsyncMock()
    session.exec = AsyncMock(return_value=result)
    source = Document(
        title="Broken",
        artifact_id=uuid7(),
        artifact_content_id=uuid7(),
        content_hash=uuid8(),
        created_by=uuid5(),
        scopes=[uuid5()],
    )

    with pytest.raises(NotVisibleError, match="original artifact"):
        dbutil.run(Artifact.share(session, source, uuid5(), [uuid5()]))


def test_promote_shares_artifact_metadata_without_copying_its_blob() -> None:
    async def probe() -> tuple[int, int, int, int, bool]:
        await dbutil.reset_db()
        owner, team = uuid5(), uuid5()
        blob = Blob(
            content_hash=uuid8(),
            size=12,
            stored_size=8,
            storage_key="objects/shared",
        )
        artifact = Artifact(name="contract.pdf", created_by=owner, scopes=[owner])
        async with User.private(owner) as session:
            session.add_all((blob, artifact))
            await session.flush()
            content = Artifact.Content(
                artifact_id=artifact.id,
                blob_id=blob.id,
                state=Artifact.Content.State.ready,
                markdown="Contract terms",
                created_by=owner,
                scopes=[owner],
            )
            session.add(content)
            await session.flush()
            documents = [
                Document(
                    title=f"Contract {number}",
                    content_hash=uuid8(),
                    artifact_id=artifact.id,
                    artifact_content_id=content.id,
                    created_by=owner,
                    scopes=[owner],
                    chunks=[Chunk(ord=0, text="Contract terms", created_by=owner, scopes=[owner])],
                )
                for number in (1, 2)
            ]
            session.add_all(documents)
        user = User.authorized(owner, read=(owner, team), write=(owner, team))
        shared = await promote([document.id for document in documents], frozenset({team}), user)
        async with User.system((owner, team)).owner as session:
            blobs = (await session.exec(select(Blob.id.count()))).one()
            artifacts = (await session.exec(select(Artifact.id.count()))).one()
            contents = (await session.exec(select(Artifact.Content.id.count()))).one()
            target_documents = (
                await session.exec(select(Document).where(Document.scopes == [team]))
            ).all()
            target_contents = {document.artifact_content_id for document in target_documents}
        return shared, blobs, artifacts, contents, len(target_contents) == 1

    assert dbutil.run(probe()) == (2, 1, 2, 2, True)
