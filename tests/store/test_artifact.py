from datetime import UTC, datetime, timedelta

import dbutil
import pytest
import rls
from factories import seed_artifact
from id_factory import uuid5, uuid7, uuid8
from pydantic import UUID5, UUID7, UUID8, ValidationError
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import select

from aizk.artifacts import ArtifactRepository, OriginalDescription
from aizk.storage import IntegrityCheck, StoredBytes
from aizk.store import Artifact, Blob, Document
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


def stored_bytes(key: str, digest: UUID8, size: int) -> StoredBytes:
    """Build uncompressed object metadata for repository integration tests."""
    return StoredBytes(
        key=key,
        content_hash=digest,
        size=size,
        stored_size=size,
        encoding=Blob.Encoding.identity,
    )


def test_artifact_models_keep_bytes_outside_postgres() -> None:
    owner = uuid5()
    blob = Blob(
        content_hash=uuid8(),
        size=4,
        stored_size=4,
        storage_key="objects/a",
        media_type="text/plain",
    )
    artifact = Artifact(name="a.txt", created_by=owner, scopes=[owner])
    content = Artifact.Content(
        artifact_id=artifact.id,
        blob_id=blob.id,
        created_by=owner,
        scopes=[owner],
    )

    assert {column.name for column in Blob.__table__.columns} == {
        "id",
        "content_hash",
        "size",
        "stored_size",
        "encoding",
        "storage_key",
        "storage_version",
        "media_type",
        "etag",
        "integrity_checked_at",
        "integrity_error",
        "created_at",
    }
    assert content.state is Artifact.Content.State.pending
    assert content.revision == 1
    assert content.companion_text is None
    assert content.markdown is None
    assert content.docling_json is None
    assert artifact.contents == []
    assert artifact.promoted_from is None


def test_document_links_an_artifact_without_replacing_external_provenance() -> None:
    owner = uuid5()
    artifact = Artifact(name="paper.pdf", created_by=owner, scopes=[owner])
    artifact_content_id = uuid7()
    document = Document(
        title="Paper",
        source_uri="https://papers.test/paper.pdf",
        artifact_id=artifact.id,
        artifact_content_id=artifact_content_id,
        content_hash=uuid8(),
        created_by=owner,
        scopes=[owner],
    )

    assert document.artifact_id == artifact.id
    assert document.artifact_content_id == artifact_content_id
    assert document.source_uri == "https://papers.test/paper.pdf"
    artifact_targets = {
        foreign_key.target_fullname
        for foreign_key in Document.__table__.foreign_keys
        if foreign_key.parent.name == "artifact_id"
    }
    assert "artifact.id" in artifact_targets
    # The content revision is bound to its artifact through one composite foreign key so a
    # forged pair cannot smuggle a content row belonging to a different artifact.
    pair = next(
        constraint
        for constraint in Document.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and {element.parent.name for element in constraint.elements}
        == {"artifact_id", "artifact_content_id"}
    )
    assert {element.target_fullname for element in pair.elements} == {
        "artifact_content.artifact_id",
        "artifact_content.id",
    }
    assert all(element.ondelete == "SET NULL" for element in pair.elements)


@pytest.mark.parametrize(
    ("field", "value"),
    [("content_hash", uuid5()), ("size", -1), ("stored_size", -1), ("storage_key", "")],
)
def test_blob_rejects_invalid_integrity_metadata(field: str, value: UUID5 | int | str) -> None:
    values: dict[str, UUID5 | UUID8 | int | str] = {
        "content_hash": uuid8(),
        "size": 1,
        "stored_size": 1,
        "storage_key": "objects/valid",
        field: value,
    }
    with pytest.raises(ValidationError):
        Blob.model_validate(values)


def test_blob_is_readable_and_mintable_but_immutable() -> None:
    policies = Blob.__rls__()
    assert {policy.command for policy in policies} == {rls.Command.select, rls.Command.insert}
    assert {policy.name for policy in policies} == {"blob_read", "blob_insert"}


def test_blob_rejects_a_stored_representation_larger_than_its_original() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        with pytest.raises(DBAPIError, match="ck_blob_stored_size_bounded"):
            async with User.private(uuid5()) as session:
                session.add(
                    Blob(
                        content_hash=uuid8(),
                        size=1,
                        stored_size=2,
                        storage_key="objects/expanded",
                    )
                )

    dbutil.run(body())


def test_artifact_visibility_carries_through_contents_to_blob() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner, outsider, organization = (uuid5() for _ in range(3))
        stored = await seed_artifact(owner, [organization])

        async def visible(user: User) -> tuple[bool, bool, bool]:
            async with user as session:
                artifact = await session.scalar(
                    select(Artifact.id).where(Artifact.id == stored.artifact.id)
                )
                content = await session.scalar(
                    select(Artifact.Content.id).where(Artifact.Content.id == stored.content.id)
                )
                blob = await session.scalar(select(Blob.id).where(Blob.id == stored.blob.id))
                return artifact is not None, content is not None, blob is not None

        assert await visible(User.authorized(owner, read=(organization,))) == (True, True, True)
        assert await visible(User.private(outsider)) == (False, False, False)
        assert await visible(User.authorized(outsider, public=(organization,))) == (
            True,
            True,
            True,
        )

    dbutil.run(body())


def test_artifact_content_write_requires_the_parent_scope() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        victim, attacker = uuid5(), uuid5()
        stored = await seed_artifact(victim, [victim])
        blob = Blob(
            content_hash=uuid8(),
            size=1,
            stored_size=1,
            storage_key=f"objects/{uuid5()}",
        )

        with pytest.raises(DBAPIError, match="row-level security"):
            async with User.private(attacker) as session:
                session.add(blob)
                await session.flush()
                session.add(
                    Artifact.Content(
                        artifact_id=stored.artifact.id,
                        blob_id=blob.id,
                        created_by=attacker,
                        scopes=[attacker],
                    )
                )

    dbutil.run(body())


def test_attaching_a_foreign_blob_is_blocked_and_leaks_nothing() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        victim, attacker = uuid5(), uuid5()
        stored = await seed_artifact(victim, [victim])

        # The attacker owns an artifact in their scope and names the victim's blob id on a
        # writable content row. Without the guard this would expose the blob because the
        # read policy surfaces any blob referenced by visible content.
        with pytest.raises(DBAPIError, match="not attachable"):
            async with User.private(attacker) as session:
                mine = Artifact(name="mine.txt", created_by=attacker, scopes=[attacker])
                session.add(mine)
                await session.flush()
                session.add(
                    Artifact.Content(
                        artifact_id=mine.id,
                        blob_id=stored.blob.id,
                        created_by=attacker,
                        scopes=[attacker],
                    )
                )
                await session.flush()

        async with User.private(attacker) as session:
            leaked = await session.scalar(select(Blob.id).where(Blob.id == stored.blob.id))
        assert leaked is None

    dbutil.run(body())


def test_a_visible_blob_can_be_reattached_into_a_writable_target() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner, organization = uuid5(), uuid5()
        stored = await seed_artifact(owner, [owner])

        # The share path reattaches an already-visible blob into a scope the caller may
        # write. The blob is reachable through the caller's own content, so the guard
        # allows it.
        async with User.authorized(
            owner, read=[owner, organization], write=[owner, organization]
        ) as session:
            shared_artifact = Artifact(
                name="notes.txt",
                created_by=owner,
                scopes=[organization],
                promoted_from=stored.artifact.id,
            )
            session.add(shared_artifact)
            await session.flush()
            session.add(
                Artifact.Content(
                    artifact_id=shared_artifact.id,
                    blob_id=stored.blob.id,
                    created_by=owner,
                    scopes=[organization],
                )
            )
            await session.flush()
            attached = await session.scalar(
                select(Artifact.Content.id).where(
                    Artifact.Content.artifact_id == shared_artifact.id,
                    Artifact.Content.blob_id == stored.blob.id,
                )
            )
        assert attached is not None

    dbutil.run(body())


def test_content_blob_id_is_immutable_after_insert() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        stored = await seed_artifact(owner, [owner])

        with pytest.raises(DBAPIError, match="immutable"):
            async with User.private(owner) as session:
                replacement = Blob(
                    content_hash=uuid8(),
                    size=1,
                    stored_size=1,
                    storage_key=f"objects/{uuid5()}",
                )
                session.add(replacement)
                await session.flush()
                content = await session.get(Artifact.Content, stored.content.id)
                assert content is not None
                content.blob_id = replacement.id
                await session.flush()

    dbutil.run(body())


def source_document(owner: UUID5, artifact_id: UUID7, artifact_content_id: UUID7) -> Document:
    """A transient document carrier naming one artifact revision for `Artifact.share`."""
    return Document(
        title="carrier",
        artifact_id=artifact_id,
        artifact_content_id=artifact_content_id,
        content_hash=uuid8(),
        created_by=owner,
        scopes=[owner],
    )


def test_share_rejects_a_pair_whose_content_belongs_to_another_artifact() -> None:
    from aizk.exceptions import NotVisibleError

    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        first = await seed_artifact(owner, [owner], name="first.txt")
        second = await seed_artifact(owner, [owner], name="second.txt")
        forged = source_document(owner, first.artifact.id, second.content.id)
        with pytest.raises(NotVisibleError, match="not visible"):
            async with User.private(owner) as session:
                await Artifact.share(session, forged, owner, [owner])

    dbutil.run(body())


def test_share_reuses_the_target_artifact_and_versions_new_blobs() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner, organization = uuid5(), uuid5()
        first = await seed_artifact(owner, [owner], name="doc.txt", source_uri="s://doc")
        second_blob = Blob(
            content_hash=uuid8(),
            size=2,
            stored_size=2,
            storage_key=f"objects/{uuid5()}",
        )
        async with User.private(owner) as session:
            session.add(second_blob)
            await session.flush()
            session.add(
                Artifact.Content(
                    artifact_id=first.artifact.id,
                    blob_id=second_blob.id,
                    revision=2,
                    created_by=owner,
                    scopes=[owner],
                )
            )

        user = User.authorized(owner, read=[owner, organization], write=[owner, organization])
        target = sorted({organization})
        first_source = source_document(owner, first.artifact.id, first.content.id)
        async with user as session:
            second_content_id = await session.scalar(
                select(Artifact.Content.id).where(Artifact.Content.blob_id == second_blob.id)
            )
            assert second_content_id is not None
            second_source = source_document(owner, first.artifact.id, second_content_id)
            artifact_a, first_shared = await Artifact.share(session, first_source, owner, target)
            artifact_b, second_shared = await Artifact.share(session, second_source, owner, target)
            # The dedup by (artifact, blob) makes resharing the same source idempotent.
            artifact_c, again = await Artifact.share(session, first_source, owner, target)
            revisions = sorted(
                await session.exec(
                    select(Artifact.Content.revision).where(
                        Artifact.Content.artifact_id == artifact_a
                    )
                )
            )

        assert artifact_a == artifact_b == artifact_c
        assert first_shared != second_shared
        assert again == first_shared
        assert revisions == [1, 2]

    dbutil.run(body())


def test_share_copies_complete_models_and_canonicalizes_target_scopes() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner, first_scope, second_scope = uuid5(), uuid5(), uuid5()
        stored = await seed_artifact(owner, [owner], name="source.txt", source_uri="s://source")
        observed = datetime(2026, 7, 2, tzinfo=UTC)
        expires = datetime(2027, 7, 2, tzinfo=UTC)
        async with User.private(owner) as session:
            artifact = await session.get(Artifact, stored.artifact.id)
            content = await session.get(Artifact.Content, stored.content.id)
            assert artifact is not None and content is not None
            artifact.description = "Preserved description"
            content.state = Artifact.Content.State.ready
            content.companion_text = "Companion context"
            content.markdown = "# Converted\n"
            content.docling_json = {"schema_name": "DoclingDocument"}
            content.details = {"status": "success"}
            content.observed_at = observed
            content.expires_at = expires
            content.processed_at = observed

        user = User.authorized(
            owner,
            read=[owner, first_scope, second_scope],
            write=[owner, first_scope, second_scope],
        )
        source = source_document(owner, stored.artifact.id, stored.content.id)
        async with user as session:
            artifact_id, content_id = await Artifact.share(
                session,
                source,
                owner,
                [second_scope, first_scope, second_scope],
            )
            assert artifact_id is not None and content_id is not None
            original_artifact = await session.get(Artifact, stored.artifact.id)
            original_content = await session.get(Artifact.Content, stored.content.id)
            copied_artifact = await session.get(Artifact, artifact_id)
            copied_content = await session.get(Artifact.Content, content_id)
            assert all(
                row is not None
                for row in (original_artifact, original_content, copied_artifact, copied_content)
            )
            assert original_artifact is not None and copied_artifact is not None
            assert original_content is not None and copied_content is not None
            artifact_fields = {"name", "description", "source_uri"}
            content_identity = {
                "id",
                "artifact_id",
                "revision",
                "created_at",
                "updated_at",
                "created_by",
                "scopes",
            }
            assert copied_artifact.model_dump(include=artifact_fields) == (
                original_artifact.model_dump(include=artifact_fields)
            )
            assert copied_content.model_dump(exclude=content_identity) == (
                original_content.model_dump(exclude=content_identity)
            )
            assert (
                copied_artifact.scopes
                == copied_content.scopes
                == sorted((first_scope, second_scope), key=str)
            )

    dbutil.run(body())


def test_processing_state_is_mutable_but_blob_metadata_is_not() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        stored = await seed_artifact(owner, [owner])

        async with User.private(owner) as session:
            content = await session.get(Artifact.Content, stored.content.id)
            assert content is not None
            content.state = Artifact.Content.State.ready

        with pytest.raises(StaleDataError, match="0 were matched"):
            async with User.private(owner) as session:
                blob = await session.get(Blob, stored.blob.id)
                assert blob is not None
                blob.etag = "changed"

    dbutil.run(body())


def test_repository_versions_one_uri_and_persists_derivatives_in_postgres() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        scopes = frozenset({owner})
        user = User.private(owner)
        repository = ArtifactRepository()
        observed = datetime(2026, 7, 1, tzinfo=UTC)
        stored = stored_bytes("objects/original-1", uuid8(), 1).model_copy(
            update={"etag": "first-etag", "version": "first-version"}
        )
        first = await repository.create_original(
            user,
            stored,
            OriginalDescription(
                filename="paper.pdf",
                media_type="application/pdf",
                source_uri="https://example.com/paper.pdf",
                companion_text="The first revision.",
                observed_at=observed,
            ),
            scopes,
        )
        second = await repository.create_original(
            user,
            stored_bytes("objects/original-2", uuid8(), 2),
            OriginalDescription(
                filename="paper-v2.pdf",
                media_type="application/pdf",
                source_uri="https://example.com/paper.pdf",
                expires_at=observed,
            ),
            scopes,
        )

        assert first.artifact_id == second.artifact_id
        current = await repository.original(user, second.content_id, scopes)
        assert current.revision == 2
        assert current.filename == "paper-v2.pdf"
        assert current.size == 2
        assert current.expires_at == observed
        assert current.observed_at is None
        assert await repository.pending(user, scopes, limit=1) == (first.content_id,)

        await repository.store_conversion(
            user,
            current,
            "# Paper\n",
            {"texts": [{"text": "before\x00after"}]},
            {"status": "success", "pages": 1, "nul\x00key": "nul\x00value"},
        )
        await repository.set_state(
            user,
            second.content_id,
            scopes,
            Artifact.Content.State.ready,
        )
        assert await repository.pending(user, scopes, limit=100) == (first.content_id,)

        async with user as session:
            contents = (
                await session.exec(
                    select(Artifact.Content).where(
                        Artifact.Content.artifact_id == second.artifact_id
                    )
                )
            ).all()
        assert [item.revision for item in contents] == [1, 2]
        original = next(item for item in contents if item.id == second.content_id)
        assert original.markdown == "# Paper\n"
        assert original.docling_json == {"texts": [{"text": "before\ufffdafter"}]}
        assert original.details == {
            "status": "success",
            "pages": 1,
            "nul\ufffdkey": "nul\ufffdvalue",
        }
        assert original.state is Artifact.Content.State.ready
        assert original.processed_at is not None
        async with User.system().owner as session:
            blob = await session.scalar(select(Blob).where(Blob.storage_key == stored.key))
        assert blob is not None
        assert blob.model_dump(include={"etag", "storage_version"}) == {
            "etag": stored.etag,
            "storage_version": stored.version,
        }

    dbutil.run(body())


def test_repository_tracks_bounded_object_integrity_passes() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        stored = await seed_artifact(owner, [owner])
        repository = ArtifactRepository()
        checked_at = datetime.now(UTC)

        candidates = await repository.integrity_candidates(checked_at, limit=1)
        assert len(candidates) == 1
        assert candidates[0].id == stored.blob.id
        assert candidates[0].key == stored.blob.storage_key

        await repository.record_integrity((), checked_at)
        await repository.record_integrity((IntegrityCheck(id=stored.blob.id),), checked_at)
        assert await repository.integrity_candidates(checked_at - timedelta(days=1), limit=1) == ()

        error = "IntegrityMismatch: changed bytes"
        await repository.record_integrity(
            (IntegrityCheck(id=stored.blob.id, error=error),),
            checked_at,
        )
        assert (await repository.integrity_candidates(checked_at, limit=1))[0].id == stored.blob.id
        async with User.system().owner as session:
            blob = await session.get(Blob, stored.blob.id)
            assert blob is not None
            assert blob.integrity_checked_at == checked_at
            assert blob.integrity_error == error

        with pytest.raises(LookupError, match="disappeared"):
            await repository.record_integrity((IntegrityCheck(id=uuid7()),), checked_at)

    dbutil.run(body())


def test_repository_rejects_mismatched_queue_scopes_and_missing_content() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner, extra = uuid5(), uuid5()
        scopes = frozenset({owner})
        user = User.private(owner)
        repository = ArtifactRepository()
        receipt = await repository.create_original(
            user,
            stored_bytes("objects/original", uuid8(), 1),
            OriginalDescription(filename="paper.pdf", media_type="application/pdf"),
            scopes,
        )
        original = await repository.original(user, receipt.content_id, scopes)

        with pytest.raises(LookupError, match="original is not visible"):
            await repository.original(user, uuid7(), scopes)
        with pytest.raises(PermissionError, match="scopes"):
            await repository.original(
                User.system((owner, extra)),
                receipt.content_id,
                frozenset({owner, extra}),
            )
        with pytest.raises(LookupError, match="conversion scopes"):
            await repository.store_conversion(
                user,
                original.model_copy(update={"content_id": uuid7()}),
                "text",
                {},
                {},
            )
        with pytest.raises(LookupError, match="queued scopes"):
            await repository.set_state(
                user,
                uuid7(),
                scopes,
                Artifact.Content.State.failed,
                "missing",
            )

    dbutil.run(body())
