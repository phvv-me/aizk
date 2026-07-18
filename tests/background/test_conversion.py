import asyncio
from types import TracebackType
from typing import Self

import pytest
from id_factory import uuid5, uuid7
from pydantic import UUID7

import aizk.background.jobs.conversion as conversion_module
from aizk.background.jobs.conversion import ArtifactQueue, DoclingConversionJob
from aizk.background.jobs.models import ArtifactConversionJob
from aizk.config import settings
from aizk.types import Scopes


class Processor:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID7, Scopes]] = []

    async def process(self, content_id: UUID7, scopes: Scopes) -> None:
        self.calls.append((content_id, scopes))


class Queue:
    def __init__(self, admitted: bool) -> None:
        self.admitted = admitted
        self.enqueued: list[tuple[ArtifactConversionJob, str]] = []
        self.requeued = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def enqueue(
        self,
        job: DoclingConversionJob,
        payload: ArtifactConversionJob,
        dedupe_key: str,
    ) -> bool:
        del job
        self.enqueued.append((payload, dedupe_key))
        return self.admitted

    async def requeue_failed(self, job: DoclingConversionJob, limit: int = 100) -> int:
        del job, limit
        self.requeued += 1
        return 1


def test_conversion_job_delegates_the_durable_original_to_the_processor() -> None:
    processor = Processor()
    payload = ArtifactConversionJob(
        artifact_content_id=uuid7(),
        scopes=frozenset({uuid5()}),
    )
    job = DoclingConversionJob(processor)

    asyncio.run(job.handle(payload))

    assert processor.calls == [(payload.artifact_content_id, payload.scopes)]
    assert job.entrypoint == "aizk_convert_artifact"
    assert job.priority == 75
    assert job.concurrency_limit == settings.docling_concurrency


@pytest.mark.parametrize("admitted", [True, False])
def test_artifact_queue_enqueues_only_ids_and_recovers_a_held_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    admitted: bool,
) -> None:
    connection = Queue(admitted)
    monkeypatch.setattr(conversion_module, "Queue", lambda dsn: connection)
    content_id, scopes = uuid7(), frozenset({uuid5()})

    result = asyncio.run(
        ArtifactQueue(DoclingConversionJob(Processor())).enqueue(content_id, scopes)
    )

    assert result is admitted
    [(payload, dedupe_key)] = connection.enqueued
    assert payload == ArtifactConversionJob(artifact_content_id=content_id, scopes=scopes)
    assert dedupe_key == str(content_id)
    assert connection.requeued == int(not admitted)
