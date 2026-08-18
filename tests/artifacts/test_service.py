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

from aizk.artifacts.boilerplate import WebBoilerplateCleaner
from aizk.artifacts.description import (
    CaptionAttempt,
    CaptionUsage,
    DescribedArtifact,
    FigureDescription,
    ImageCaption,
)
from aizk.artifacts.models import (
    ArtifactDocument,
    ArtifactReceipt,
    ConvertedArtifact,
    OriginalArtifact,
    OriginalDescription,
)
from aizk.artifacts.repository import ArtifactRepository
from aizk.artifacts.service import (
    ArtifactIntake,
    ArtifactIntegrity,
    ArtifactProcessor,
    ArtifactReindexer,
    _resolve_markdown_links,
)
from aizk.artifacts.visual import VisualModality
from aizk.extract.ingest import TextIngestor, TextSource
from aizk.integrations.clamav import ClamAVClient, CleanScan
from aizk.integrations.converter import ArtifactConverter
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
            tuple[OriginalArtifact, str, datetime, list[dict[str, JsonValue]]]
        ] = []
        self.candidates: list[tuple[OriginalArtifact, str, str, str | None]] = []
        self.candidate_errors: list[tuple[OriginalArtifact, str, str]] = []
        self.converted_value: ConvertedArtifact | None = None
        self.indexings: list[tuple[UUID7, datetime]] = []
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
    ) -> tuple[IntegrityCheck, ...]:
        self.integrity_checks = checks
        self.integrity_checked_at = checked_at
        return checks

    async def record_candidate(
        self,
        user: User,
        original: OriginalArtifact,
        markdown: str,
        policy: str,
        error: str | None = None,
    ) -> None:
        del user
        self.candidates.append((original, markdown, policy, error))

    async def record_candidate_error(
        self,
        user: User,
        original: OriginalArtifact,
        policy: str,
        error: str,
    ) -> None:
        del user
        self.candidate_errors.append((original, policy, error))

    async def promote_candidate(
        self,
        user: User,
        original: OriginalArtifact,
        policy: str,
        indexed_at: datetime,
        caption_metadata: list[dict[str, JsonValue]],
    ) -> None:
        del user
        candidate = self.candidates[-1]
        assert candidate[0] == original
        assert candidate[2] == policy
        self.conversions.append((original, candidate[1], indexed_at, caption_metadata))

    async def converted(
        self,
        user: User,
        content_id: UUID7,
        scopes: Scopes,
    ) -> ConvertedArtifact:
        del user, content_id, scopes
        assert self.converted_value is not None
        return self.converted_value

    async def record_indexing(self, user: User, content_id: UUID7, indexed_at: datetime) -> None:
        del user
        self.indexings.append((content_id, indexed_at))


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


class Description:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[OriginalArtifact, bytes, str]] = []

    async def enrich(
        self,
        original: OriginalArtifact,
        content: bytes,
        markdown: str,
    ) -> DescribedArtifact:
        self.events.append("description")
        self.calls.append((original, content, markdown))
        caption = ImageCaption(
            text="The chart loss falls with expert count.",
            requested_model="gemma",
            model="gemma",
            provider="CoreWeave",
            elapsed_ms=10,
            usage=CaptionUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            attempts=(
                CaptionAttempt(
                    requested_model="gemma",
                    attempt=1,
                    elapsed_ms=10,
                    status_code=200,
                ),
            ),
        )
        return DescribedArtifact(
            markdown=f"{markdown.rstrip()}\n\n{caption.text}\n",
            figures=(
                FigureDescription(
                    ordinal=0,
                    image_sha256="a" * 64,
                    media_type="image/png",
                    caption=caption,
                ),
            ),
        )


def docling_response(markdown: str = "# Paper") -> DoclingResponse:
    return DoclingResponse.model_validate(
        {
            "document": {"md_content": markdown},
            "status": "success",
            "processing_time": 1.0,
        }
    )


@pytest.mark.parametrize(
    ("markdown", "source_uri", "expected"),
    [
        ("[Rule](child)", None, "[Rule](child)"),
        ("[Rule](child)", "vault:///notes/index.md", "[Rule](child)"),
        (
            "[Rule](child) [Web](https://example.org/rule)",
            "https://docs.example/python/",
            "[Rule](https://docs.example/python/child) [Web](https://example.org/rule)",
        ),
        (
            "[Rule](<../child>)\n\n[more]: #details",
            "https://docs.example/python/index.html",
            "[Rule](<https://docs.example/child>)\n\n"
            "[more]: https://docs.example/python/index.html#details",
        ),
    ],
)
def test_converted_markdown_resolves_only_http_source_relative_links(
    markdown: str, source_uri: str | None, expected: str
) -> None:
    assert _resolve_markdown_links(markdown, source_uri) == expected


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
    artifact = ArtifactBytes(
        content=b"%PDF-1.7 paper", filename="paper.pdf", media_type="application/pdf"
    )

    receipt = asyncio.run(
        intake(scanner, storage, repository, enqueuer).accept(
            user,
            artifact,
            target=frozenset({owner}),
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )

    assert scanner.scanned == [b"%PDF-1.7 paper"]
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
                    content=b"%PDF-1.7 pdf",
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
                    content=b"%PDF-1.7 paper", filename="paper.pdf", media_type="application/pdf"
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
            stored_size=len(valid),
            encoding=Blob.Encoding.identity,
        ),
        StoredObject(
            id=missing_id,
            key="objects/missing",
            content_hash=sql.uuid8(b"different"),
            size=1,
            stored_size=1,
            encoding=Blob.Encoding.identity,
        ),
    )
    integrity = ArtifactIntegrity(
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    report = asyncio.run(integrity.verify(limit=2, interval_days=30))

    assert report.model_dump() == {"checked": 2, "valid": 1, "failed": 1}
    assert repository.integrity_checks[0] == IntegrityCheck(
        observed=repository.integrity_objects[0]
    )
    assert repository.integrity_checks[1].observed.id == missing_id
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


def test_processor_retries_a_stale_compaction_pointer_through_the_current_layout() -> None:
    source = original()
    current = source.model_copy(
        update={
            "storage_key": "objects/current-layout",
            "storage_version": "current-version",
        }
    )
    storage = Storage()
    storage.values[current.storage_key] = b"original"
    repository = Repository(current)
    processor = ArtifactProcessor(
        cast(ArtifactConverter, None),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    refreshed, content = asyncio.run(
        processor.read_original(
            User.system(source.scopes),
            source,
            source.content_id,
            source.scopes,
        )
    )

    assert refreshed == current
    assert content == b"original"
    assert storage.versions == ["current-version"]


def test_processor_preserves_an_error_when_the_storage_pointer_is_current() -> None:
    source = original()
    storage = Storage()
    processor = ArtifactProcessor(
        cast(ArtifactConverter, None),
        cast(ByteStore, storage),
        cast(ArtifactRepository, Repository(source)),
    )

    with pytest.raises(FileNotFoundError, match="objects/original"):
        asyncio.run(
            processor.read_original(
                User.system(source.scopes),
                source,
                source.content_id,
                source.scopes,
            )
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


def test_processor_describes_figures_before_text_embedding_and_persists_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original()
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    events: list[str] = []
    description = Description(events)
    submitted: list[TextSource] = []

    async def ingest(ingestor: TextIngestor, text: TextSource) -> tuple[UUID7, bool]:
        del ingestor
        events.append("embedding")
        submitted.append(text)
        return uuid7(), True

    async def enqueue(document_id: UUID7, scopes: Scopes) -> int:
        del document_id, scopes
        events.append("projection")
        return 1

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", ingest)
    monkeypatch.setattr("aizk.artifacts.service.enqueue_document", enqueue)
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(docling_response("# Paper\n\nRaw figure"))),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
        description=description,
    )

    asyncio.run(processor.process(source.content_id, source.scopes))

    assert events == ["description", "embedding", "projection"]
    assert description.calls == [(source, b"original", "# Paper\n\nRaw figure\n")]
    assert "The chart loss falls" in submitted[0].text
    metadata = repository.conversions[0][3]
    restored = FigureDescription.model_validate(metadata[0])
    assert restored.caption.provider == "CoreWeave"
    assert restored.caption.usage.total_tokens == 5


def test_processor_stores_postgres_derivatives_and_makes_one_file_document_findable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original()
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    converter = Converter(
        docling_response(
            "# Paper\n\n[Local rule](py-clean-code) and [external](https://example.org/rule)."
        )
    )
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
    assert repository.conversions[0][1] == (
        "# Paper\n\n[Local rule](https://files.example/py-clean-code) and "
        "[external](https://example.org/rule).\n"
    )
    assert repository.conversions[0][2].tzinfo is UTC
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


def test_processor_strips_web_chrome_only_when_a_cleaner_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original().model_copy(
        update={
            "filename": "datahub",
            "media_type": "text/html",
            "source_uri": "https://github.com/datahub-project/datahub",
        }
    )
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    page = (
        "## Navigation Menu\n\n[ Sign in ](/login)\n\n"
        "# DataHub\n\nThe hackathon plan starts from the metadata graph.\n"
    )
    ingested: list[str] = []

    async def ingest(ingestor: TextIngestor, submitted: TextSource) -> tuple[UUID7, bool]:
        del ingestor
        ingested.append(submitted.text)
        return uuid7(), True

    async def enqueue(document_id: UUID7, scopes: Scopes) -> int:
        del document_id, scopes
        return 1

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", ingest)
    monkeypatch.setattr("aizk.artifacts.service.enqueue_document", enqueue)
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(docling_response(page))),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
        None,
        WebBoilerplateCleaner(),
    )

    asyncio.run(processor.process(source.content_id, source.scopes))

    stored = repository.conversions[0][1]
    assert stored == "# DataHub\n\nThe hackathon plan starts from the metadata graph.\n"
    assert "Sign in" not in ingested[0]

    processor.cleaner = None
    asyncio.run(processor.process(source.content_id, source.scopes))

    assert "[ Sign in ](https://github.com/login)" in repository.conversions[1][1]


def test_reconversion_quarantines_layout_inflation_without_replacing_production() -> None:
    current = "\n".join(["申請書の日本語本文です。"] * 20)
    source = original().model_copy(update={"markdown": current})
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    inflated = "\n".join([current] * 3)
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(docling_response(inflated))),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    asyncio.run(processor.process(source.content_id, source.scopes, "japanese-ocr-v2"))

    assert repository.conversions == []
    assert repository.candidates[0][1] == f"{inflated}\n"
    assert repository.candidates[0][2] == "japanese-ocr-v2"
    assert (repository.candidates[0][3] or "").startswith("candidate length inflated 3.")
    assert [state[2] for state in repository.states] == [
        Artifact.Content.State.processing,
        Artifact.Content.State.ready,
    ]


def test_failed_candidate_promotion_restores_the_live_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = original().model_copy(update={"markdown": "# Current production text"})
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    ingested: list[str] = []

    async def ingest(ingestor: TextIngestor, submitted: TextSource) -> tuple[UUID7, bool]:
        del ingestor
        ingested.append(submitted.text)
        return uuid7(), True

    async def reject_promotion(
        user: User,
        original: OriginalArtifact,
        policy: str,
        indexed_at: datetime,
        caption_metadata: list[dict[str, JsonValue]],
    ) -> None:
        del user, original, policy, indexed_at, caption_metadata
        raise SQLAlchemyError("promotion failed")

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", ingest)
    repository.promote_candidate = reject_promotion
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(docling_response("# Better candidate text"))),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    with pytest.raises(SQLAlchemyError, match="promotion failed"):
        asyncio.run(processor.process(source.content_id, source.scopes, "test-v2"))

    assert "Better candidate" in ingested[0]
    assert "Current production" in ingested[1]
    assert repository.candidate_errors[0][1:] == ("test-v2", "promotion failed")
    assert repository.states[-1][2] is Artifact.Content.State.ready


def test_reconversion_rejection_preserves_the_live_derivative() -> None:
    source = original().model_copy(update={"markdown": "# Current production text"})
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    response = DoclingResponse.model_validate(
        {"document": {}, "status": "failure", "errors": [{"message": "unsupported"}]}
    )
    processor = ArtifactProcessor(
        cast(DoclingClient, Converter(response)),
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    asyncio.run(processor.process(source.content_id, source.scopes, "test-v2"))

    assert repository.candidate_errors[0][1] == "test-v2"
    assert repository.states[-1][2] is Artifact.Content.State.ready

    storage.fail_get = True
    with pytest.raises(ByteLimitExceeded):
        asyncio.run(processor.process(source.content_id, source.scopes, "test-v3"))
    assert repository.candidate_errors[-1][1:] == ("test-v3", "too large")
    assert repository.states[-1][2] is Artifact.Content.State.ready


def test_processor_keeps_metadata_findable_and_marks_conversion_failures(
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

    async def reject_markdown(
        user: User,
        original: OriginalArtifact,
        markdown: str,
        policy: str,
        error: str | None = None,
    ) -> None:
        del user, original, markdown, policy, error
        raise SQLAlchemyError("unsupported Unicode escape sequence")

    repository.record_candidate = reject_markdown
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


def test_docling_policy_refusal_is_marked_unreadable_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover binary and archive originals from the object store regression case.

    Docling's own policy check refuses these deterministically, so a retry only repeats the
    same verdict, and the processor records a state the retry query stops offering back."""
    source = original().model_copy(
        update={"filename": "archive.zip", "media_type": "application/zip"}
    )
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"
    response = DoclingResponse.model_validate(
        {
            "document": {},
            "status": "skipped",
            "errors": [
                {"category": "policy", "error_message": "File format not allowed: archive.zip"}
            ],
        }
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

    assert "Conversion state unreadable" in ingested[0]
    assert repository.states[-1][2:] == (
        Artifact.Content.State.unreadable,
        "File format not allowed: archive.zip",
    )


def test_group_b_regression_html_originals_convert_once_docling_sees_the_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover extensionless HTML from the object store regression case.

    A display name with no extension comes back as Docling `skipped` until the real
    `DoclingClient` renames the wire copy before sending it.

    The mock transport plays Docling's own real behavior, keyed on the sent filename alone,
    so this proves the fix through the real client rather than a test double that never saw
    the bug.
    """

    async def docling(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        if b'filename="artifact.html"' in body:
            return httpx.Response(
                200,
                json={
                    "document": {"md_content": "# Real page\n"},
                    "status": "success",
                    "errors": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "document": {},
                "status": "skipped",
                "errors": [
                    {"category": "policy", "error_message": "File format not allowed: artifact"}
                ],
            },
        )

    converter = DoclingClient(
        http=httpx.AsyncClient(
            base_url="http://docling.test/",
            transport=httpx.MockTransport(docling),
        )
    )
    source = original().model_copy(update={"filename": "artifact", "media_type": "text/html"})
    storage, repository = Storage(), Repository(source)
    storage.values[source.storage_key] = b"original"

    async def ingest(ingestor: TextIngestor, submitted: TextSource) -> tuple[UUID7, bool]:
        del ingestor, submitted
        return uuid7(), True

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.ingest", ingest)
    processor = ArtifactProcessor(
        converter,
        cast(ByteStore, storage),
        cast(ArtifactRepository, repository),
    )

    asyncio.run(processor.process(source.content_id, source.scopes))

    assert repository.states[-1][2] is Artifact.Content.State.ready
    assert repository.conversions[0][1] == "# Real page\n"


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


def converted(markdown: str = "# Paper\n\nThe converted body.\n") -> ConvertedArtifact:
    source = original()
    return ConvertedArtifact(
        **source.model_dump(
            include={
                "artifact_id",
                "content_id",
                "created_by",
                "scopes",
                "filename",
                "media_type",
                "size",
                "source_uri",
                "companion_text",
                "observed_at",
                "expires_at",
                "storage_hash",
            }
        ),
        markdown=markdown,
    )


def test_reindexer_rebuilds_chunks_from_stored_markdown_without_calling_docling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversion is the expensive half, so a chunking change replays only the cheap one."""
    stored = converted()
    repository = Repository()
    repository.converted_value = stored
    document_id = uuid7()
    rechunked: list[TextSource] = []
    enqueued: list[tuple[UUID7, Scopes]] = []

    async def rechunk(ingestor: TextIngestor, source: TextSource) -> UUID7:
        del ingestor
        rechunked.append(source)
        return document_id

    async def enqueue(resolved: UUID7, scopes: Scopes) -> int:
        enqueued.append((resolved, scopes))
        return 3

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.rechunk", rechunk)
    monkeypatch.setattr("aizk.artifacts.service.enqueue_document", enqueue)

    asyncio.run(
        ArtifactReindexer(cast(ArtifactRepository, repository)).reindex(
            stored.content_id, stored.scopes
        )
    )

    [submitted] = rechunked
    assert "## Extracted content" in submitted.text
    assert "The converted body." in submitted.text
    assert submitted.artifact_content_id == stored.content_id
    assert submitted.original_content_hash == stored.storage_hash
    assert submitted.capture is not None
    assert submitted.capture.observed_at == stored.observed_at
    assert enqueued == [(document_id, stored.scopes)]
    [(stamped_id, stamped_at)] = repository.indexings
    assert stamped_id == stored.content_id
    assert stamped_at.tzinfo is UTC


def test_reindexer_refuses_markdown_that_re_chunks_into_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Repository()
    repository.converted_value = converted()

    async def no_document(ingestor: TextIngestor, source: TextSource) -> None:
        del ingestor, source
        return None

    monkeypatch.setattr("aizk.artifacts.service.TextIngestor.rechunk", no_document)
    reindexer = ArtifactReindexer(cast(ArtifactRepository, repository))

    with pytest.raises(DoclingConversionError, match="no document"):
        asyncio.run(reindexer.reindex(uuid7(), frozenset({uuid5()})))
    assert repository.indexings == []
