import asyncio
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import cast

import httpx
import pytest
from id_factory import uuid5, uuid7
from patos import sql
from pydantic import UUID7, UUID8, JsonValue
from sqlalchemy.exc import SQLAlchemyError

from aizk.artifacts import (
    ArtifactDocument,
    ArtifactIntake,
    ArtifactIntegrity,
    ArtifactProcessor,
    ArtifactReceipt,
    ArtifactRepository,
    OriginalArtifact,
    OriginalDescription,
    VisualModality,
)
from aizk.extract.ingest import TextIngestor, TextSource
from aizk.integrations.clamav import ClamAVClient, CleanScan
from aizk.integrations.docling import (
    ArtifactBytes,
    ArtifactReader,
    DoclingClient,
    DoclingConversionError,
    DoclingResponse,
)
from aizk.storage import (
    ByteLimitExceeded,
    ByteStore,
    IntegrityCheck,
    StoredBytes,
    StoredObject,
)
from aizk.store import Artifact, Blob
from aizk.store.identity import User
from aizk.types import Scopes


class Scanner:
    def __init__(self) -> None:
        self.scanned: list[bytes] = []

    async def scan(self, content: bytes) -> CleanScan:
        self.scanned.append(content)
        return CleanScan(bytes_scanned=len(content))


class Storage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.versions: list[str | None] = []
        self.fail_get = False
        self.next_key = 0

    async def put(self, data: bytes) -> StoredBytes:
        key = f"objects/{self.next_key}"
        self.next_key += 1
        self.values[key] = data
        return StoredBytes(
            key=key,
            content_hash=sql.uuid8(data),
            size=len(data),
            stored_size=len(data),
            encoding=Blob.Encoding.identity,
        )

    async def get(
        self,
        key: str,
        *,
        encoding: Blob.Encoding = Blob.Encoding.identity,
        expected_size: int | None = None,
        expected_hash: UUID8 | None = None,
        version: str | None = None,
    ) -> bytes:
        if self.fail_get:
            raise ByteLimitExceeded("too large")
        try:
            data = self.values[key]
        except KeyError as missing:
            raise FileNotFoundError(key) from missing
        assert encoding is Blob.Encoding.identity
        assert expected_size == len(data)
        assert expected_hash == sql.uuid8(data)
        self.versions.append(version)
        return data

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)


class Repository:
    def __init__(self, original: OriginalArtifact | None = None) -> None:
        self.original_value = original
        self.created: list[dict] = []
        self.states: list[tuple[UUID7, Scopes, Artifact.Content.State, str | None]] = []
        self.conversions: list[
            tuple[OriginalArtifact, str, dict[str, JsonValue], dict[str, JsonValue]]
        ] = []
        self.fail_create = False
        self.pending_ids: tuple[UUID7, ...] = ()
        self.integrity_objects: tuple[StoredObject, ...] = ()
        self.integrity_checks: tuple[IntegrityCheck, ...] = ()
        self.integrity_checked_at: datetime | None = None

    async def create_original(
        self,
        user: User,
        stored: StoredBytes,
        described: OriginalDescription,
        scopes: Scopes,
    ) -> ArtifactReceipt:
        del user
        if self.fail_create:
            raise SQLAlchemyError("database unavailable")
        self.created.append({"stored": stored, "scopes": scopes} | described.model_dump())
        return ArtifactReceipt(
            artifact_id=uuid7(),
            content_id=uuid7(),
            state=Artifact.Content.State.pending,
        )

    async def set_state(
        self,
        user: User,
        content_id: UUID7,
        scopes: Scopes,
        state: Artifact.Content.State,
        error: str | None = None,
    ) -> None:
        del user
        self.states.append((content_id, scopes, state, error))

    async def pending(self, user: User, scopes: Scopes, limit: int = 100) -> tuple[UUID7, ...]:
        del user, scopes
        return self.pending_ids[:limit]

    async def original(
        self,
        user: User,
        content_id: UUID7,
        scopes: Scopes,
    ) -> OriginalArtifact:
        del user, content_id, scopes
        assert self.original_value is not None
        return self.original_value

    async def integrity_candidates(
        self,
        stale_before: datetime,
        limit: int,
    ) -> tuple[StoredObject, ...]:
        assert stale_before.tzinfo is UTC
        return self.integrity_objects[:limit]

    async def record_integrity(
        self,
        checks: tuple[IntegrityCheck, ...],
        checked_at: datetime,
    ) -> None:
        self.integrity_checks = checks
        self.integrity_checked_at = checked_at

    async def store_conversion(
        self,
        user: User,
        original: OriginalArtifact,
        markdown: str,
        docling_json: dict[str, JsonValue],
        details: dict[str, JsonValue],
    ) -> None:
        del user
        self.conversions.append((original, markdown, docling_json, details))


class Enqueuer:
    def __init__(self) -> None:
        self.queued: list[tuple[UUID7, Scopes]] = []

    async def enqueue(self, content_id: UUID7, scopes: Scopes) -> bool:
        self.queued.append((content_id, scopes))
        return True


class Converter:
    def __init__(self, response: DoclingResponse) -> None:
        self.response = response
        self.artifacts: list[ArtifactBytes] = []

    async def convert(self, artifact: ArtifactBytes) -> DoclingResponse:
        self.artifacts.append(artifact)
        return self.response


class Visual:
    modality = VisualModality.image

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.media_types: list[str] = []
        self.calls: list[tuple[UUID7, OriginalArtifact, bytes]] = []

    def supports(self, media_type: str) -> bool:
        self.media_types.append(media_type)
        return media_type.startswith("image/")

    async def enrich(
        self,
        user: User,
        document_id: UUID7,
        original: OriginalArtifact,
        content: bytes,
    ) -> None:
        del user
        self.events.append("visual")
        self.calls.append((document_id, original, content))


def docling_response(markdown: str = "# Paper") -> DoclingResponse:
    return DoclingResponse.model_validate(
        {
            "document": {"md_content": markdown, "json_content": {"texts": []}},
            "status": "success",
            "processing_time": 1.0,
        }
    )


def intake(
    scanner: Scanner,
    storage: Storage,
    repository: Repository,
    enqueuer: Enqueuer,
    reader: ArtifactReader | None = None,
) -> ArtifactIntake:
    return ArtifactIntake(
        reader
        or ArtifactReader(
            http=httpx.AsyncClient(), file_root=Path("/unused"), max_bytes=100, max_redirects=1
        ),
        cast(ClamAVClient, scanner),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
        enqueuer,
    )


def test_accept_scans_stores_and_queues_exact_authorized_scopes() -> None:
    owner, organization = uuid5(), uuid5()
    user = User.authorized(owner, write=(owner, organization))
    scanner, storage, repository, enqueuer = Scanner(), Storage(), Repository(), Enqueuer()
    artifact = ArtifactBytes(content=b"paper", filename="paper.pdf", media_type="application/pdf")

    receipt = asyncio.run(
        intake(scanner, storage, repository, enqueuer).accept(
            user,
            artifact,
            target=frozenset({owner}),
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )

    assert scanner.scanned == [b"paper"]
    assert repository.created[0]["scopes"] == frozenset({owner})
    assert repository.created[0]["observed_at"] == datetime(2026, 7, 1, tzinfo=UTC)
    assert enqueuer.queued == [(receipt.content_id, frozenset({owner}))]
    assert repository.states == [
        (receipt.content_id, frozenset({owner}), Artifact.Content.State.queued, None)
    ]
    assert receipt.state is Artifact.Content.State.queued


def test_uri_is_fetched_once_and_keeps_the_requested_provenance() -> None:
    async def resolve(host: str, port: int):
        del host, port
        return (ip_address("93.184.216.34"),)

    reader = ArtifactReader(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=b"pdf",
                    headers={"content-type": "application/pdf"},
                )
            )
        ),
        file_root=Path("/unused"),
        max_bytes=100,
        max_redirects=1,
        resolver=resolve,
    )
    repository = Repository()

    receipt = asyncio.run(
        intake(Scanner(), Storage(), repository, Enqueuer(), reader).uri(
            User.private(uuid5()),
            "https://files.example/paper.pdf",
        )
    )

    assert receipt.state is Artifact.Content.State.queued
    assert repository.created[0]["source_uri"] == "https://files.example/paper.pdf"
    assert repository.created[0]["filename"] == "paper.pdf"


def test_failed_metadata_transaction_compensates_the_stored_object() -> None:
    storage, repository = Storage(), Repository()
    repository.fail_create = True

    user = User.private(uuid5())
    with pytest.raises(SQLAlchemyError, match="unavailable"):
        asyncio.run(
            intake(Scanner(), storage, repository, Enqueuer()).accept(
                user,
                ArtifactBytes(
                    content=b"paper", filename="paper.pdf", media_type="application/pdf"
                ),
                target=user.write_scope(None),
            )
        )

    assert storage.values == {}
    assert storage.deleted == ["objects/0"]


def test_scope_authorization_happens_before_fetching_scanning_or_storage() -> None:
    scanner, storage = Scanner(), Storage()

    # `uri` resolves the write target before it reads the source, so an unauthorized
    # caller never triggers a fetch, scan, or object write.
    with pytest.raises(ValueError, match="no writable scope"):
        asyncio.run(
            intake(scanner, storage, Repository(), Enqueuer()).uri(
                User.private(uuid5()),
                "https://files.example/paper.pdf",
                scopes=["unknown"],
            )
        )

    assert scanner.scanned == []
    assert storage.values == {}


def test_pending_dispatch_requeues_each_durable_original_and_advances_state() -> None:
    repository, enqueuer = Repository(), Enqueuer()
    repository.pending_ids = (uuid7(), uuid7())
    owner = uuid5()
    service = intake(Scanner(), Storage(), repository, enqueuer)

    count = asyncio.run(service.dispatch_pending(frozenset({owner}), limit=2))

    assert count == 2
    assert enqueuer.queued == [
        (content_id, frozenset({owner})) for content_id in repository.pending_ids
    ]
    assert [state[2] for state in repository.states] == [
        Artifact.Content.State.queued,
        Artifact.Content.State.queued,
    ]


def test_integrity_pass_records_valid_and_failed_objects_without_exposing_keys() -> None:
    storage, repository = Storage(), Repository()
    valid_id, missing_id = uuid7(), uuid7()
    valid = b"valid"
    storage.values["objects/valid"] = valid
    repository.integrity_objects = (
        StoredObject(
            id=valid_id,
            key="objects/valid",
            content_hash=sql.uuid8(valid),
            size=len(valid),
            encoding=Blob.Encoding.identity,
        ),
        StoredObject(
            id=missing_id,
            key="objects/missing",
            content_hash=sql.uuid8(b"different"),
            size=1,
            encoding=Blob.Encoding.identity,
        ),
    )
    integrity = ArtifactIntegrity(
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    report = asyncio.run(integrity.verify(limit=2, interval_days=30))

    assert report.model_dump() == {"checked": 2, "valid": 1, "failed": 1}
    assert repository.integrity_checks[0] == IntegrityCheck(id=valid_id)
    assert repository.integrity_checks[1].id == missing_id
    assert repository.integrity_checks[1].error == "FileNotFoundError: objects/missing"
    assert repository.integrity_checked_at is not None


def original() -> OriginalArtifact:
    owner = uuid5()
    content = b"original"
    return OriginalArtifact(
        artifact_id=uuid7(),
        content_id=uuid7(),
        revision=2,
        created_by=owner,
        scopes=frozenset({owner}),
        filename="paper.pdf",
        media_type="application/pdf",
        size=len(content),
        source_uri="https://files.example/paper.pdf",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        storage_key="objects/original",
        storage_hash=sql.uuid8(content),
    )


def test_processor_adds_image_enrichment_after_docling_and_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original().model_copy(update={"filename": "diagram.png", "media_type": "image/png"})
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    events: list[str] = []
    visual = Visual(events)
    document_id = uuid7()

    async def ingest(ingestor: TextIngestor, submitted: TextSource) -> tuple[UUID7, bool]:
        del ingestor, submitted
        events.append("docling")
        return document_id, True

    async def enqueue(resolved: UUID7, scopes: Scopes) -> int:
        assert resolved == document_id
        assert scopes == source.scopes
        events.append("projection")
        return 1

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", ingest)
    monkeypatch.setattr("aizk.artifacts.service.enqueue_document", enqueue)
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(docling_response())),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
        visual,
    )

    asyncio.run(processor.process(source.content_id, source.scopes))

    assert events == ["docling", "visual", "projection"]
    assert visual.media_types == ["image/png"]
    assert visual.calls == [(document_id, source, b"original")]

    source = source.model_copy(update={"filename": "paper.pdf", "media_type": "application/pdf"})
    repository.original_value = source
    events.clear()
    asyncio.run(processor.process(source.content_id, source.scopes))
    assert events == ["docling", "projection"]
    assert visual.media_types[-1] == "application/pdf"
    assert len(visual.calls) == 1


def test_processor_stores_postgres_derivatives_and_makes_one_file_document_recallable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original()
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    converter = Converter(docling_response())
    ingested: list[TextSource] = []
    enqueued: list[tuple[UUID7, Scopes]] = []

    async def ingest(ingestor: TextIngestor, submitted: TextSource) -> tuple[UUID7, bool]:
        del ingestor
        ingested.append(submitted)
        return uuid7(), True

    async def enqueue(document_id: UUID7, scopes: Scopes) -> int:
        enqueued.append((document_id, scopes))
        return 1

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", ingest)
    monkeypatch.setattr("aizk.artifacts.service.enqueue_document", enqueue)
    processor = ArtifactProcessor(
        cast(DoclingClient, converter),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    asyncio.run(processor.process(source.content_id, source.scopes))

    assert converter.artifacts[0].content == b"original"
    assert repository.conversions[0][1:3] == ("# Paper\n", {"texts": []})
    assert storage.values == {source.storage_key: b"original"}
    assert "# paper.pdf" in ingested[0].text
    assert "## Extracted content" in ingested[0].text
    assert ingested[0].artifact_id == source.artifact_id
    assert ingested[0].artifact_content_id == source.content_id
    assert ingested[0].original_content_hash == source.storage_hash
    capture = ingested[0].capture
    assert capture is not None and capture.observed_at == source.observed_at
    assert enqueued[0][1] == source.scopes
    assert [state[2] for state in repository.states] == [
        Artifact.Content.State.processing,
        Artifact.Content.State.ready,
    ]


def test_processor_keeps_metadata_recallable_and_marks_conversion_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original()
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    ingested: list[str] = []
    enqueued: list[UUID7] = []

    async def ingest(ingestor: TextIngestor, submitted: TextSource) -> tuple[UUID7, bool]:
        del ingestor
        ingested.append(submitted.text)
        return uuid7(), True

    async def enqueue(document_id: UUID7, scopes: Scopes) -> int:
        del scopes
        enqueued.append(document_id)
        return 1

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", ingest)
    monkeypatch.setattr("aizk.artifacts.service.enqueue_document", enqueue)
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(docling_response(""))),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    asyncio.run(processor.process(source.content_id, source.scopes))

    assert repository.states[-1][2] is Artifact.Content.State.ready
    assert "Original size 8 bytes" in ingested[-1]
    assert enqueued == []

    storage.fail_get = True
    with pytest.raises(ByteLimitExceeded):
        asyncio.run(processor.process(source.content_id, source.scopes))
    assert repository.states[-1][2] is Artifact.Content.State.failed
    assert repository.states[-1][3] == "too large"

    async def no_document(ingestor: TextIngestor, submitted: TextSource) -> tuple[None, bool]:
        del ingestor, submitted
        return None, False

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", no_document)
    storage.fail_get = False
    processor.converter = cast(DoclingClient, Converter(docling_response()))
    with pytest.raises(DoclingConversionError, match="did not create"):
        asyncio.run(processor.process(source.content_id, source.scopes))
    assert repository.states[-1][2] is Artifact.Content.State.failed


def test_processor_persists_invalid_imported_metadata_as_a_failed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original()
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"

    async def reject_declaration(
        ingestor: TextIngestor, submitted: TextSource
    ) -> tuple[UUID7, bool]:
        del ingestor, submitted
        raise ValueError("unknown ontology entity type 'classification'")

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", reject_declaration)
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(docling_response())),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    with pytest.raises(ValueError, match="unknown ontology entity type"):
        asyncio.run(processor.process(source.content_id, source.scopes))

    assert repository.states[-1][2:] == (
        Artifact.Content.State.failed,
        "unknown ontology entity type 'classification'",
    )


def test_processor_persists_conversion_database_errors_as_a_failed_state() -> None:
    source = original()
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"

    async def reject_json(
        user: User,
        original: OriginalArtifact,
        markdown: str,
        docling_json: dict[str, JsonValue],
        details: dict[str, JsonValue],
    ) -> None:
        del user, original, markdown, docling_json, details
        raise SQLAlchemyError("unsupported Unicode escape sequence")

    repository.store_conversion = reject_json
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(docling_response())),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    with pytest.raises(SQLAlchemyError, match="unsupported Unicode escape"):
        asyncio.run(processor.process(source.content_id, source.scopes))

    assert repository.states[-1][2:] == (
        Artifact.Content.State.failed,
        "unsupported Unicode escape sequence",
    )


def test_docling_rejection_keeps_a_metadata_document_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original()
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    response = DoclingResponse.model_validate(
        {"document": {}, "status": "failure", "errors": [{"message": "unsupported"}]}
    )
    ingested: list[str] = []

    async def ingest(ingestor: TextIngestor, submitted: TextSource) -> tuple[UUID7, bool]:
        del ingestor
        ingested.append(submitted.text)
        return uuid7(), True

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", ingest)
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(response)),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    asyncio.run(processor.process(source.content_id, source.scopes))

    assert "Conversion state failed" in ingested[0]
    assert repository.states[-1][2:] == (
        Artifact.Content.State.failed,
        "Docling conversion ended with failure",
    )


def test_artifact_document_normalizes_blank_text_and_stays_non_semantic() -> None:
    document = ArtifactDocument(
        filename="notes.txt",
        media_type="text/plain",
        size=3,
        companion_text="   \n",
        markdown="\t",
        conversion_state=Artifact.Content.State.failed,
    )

    assert document.companion_text is None
    assert document.markdown is None
    assert not document.semantic
    rendered = asyncio.run(document.to_markdown())
    assert "## Extracted content" not in rendered
    assert rendered.startswith("# notes.txt\n\n## Source file")
