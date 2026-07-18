import pytest
from hypothesis import given
from id_factory import uuid5s, uuid7s
from pydantic import UUID5, UUID7

from aizk.background.jobs.models import ArtifactConversionJob, ChunkJob, MaintenanceJob

type Payload = ArtifactConversionJob | ChunkJob | MaintenanceJob


@pytest.mark.parametrize(
    ("payload_cls", "subject"),
    [
        (ChunkJob, "chunk_id"),
        (ArtifactConversionJob, "artifact_content_id"),
        (MaintenanceJob, None),
    ],
    ids=["chunk", "conversion", "task"],
)
@given(identity=uuid5s, record=uuid7s, scope=uuid5s)
def test_payload_round_trips_exactly_the_fields_the_worker_decodes(
    payload_cls: type[Payload],
    subject: str | None,
    identity: UUID5,
    record: UUID7,
    scope: UUID5,
) -> None:
    job: Payload
    expected: UUID5 | UUID7 = record
    if payload_cls is ChunkJob:
        job = ChunkJob(chunk_id=record, scopes=frozenset({scope}))
    elif payload_cls is ArtifactConversionJob:
        job = ArtifactConversionJob(artifact_content_id=record, scopes=frozenset({scope}))
    else:
        expected = identity
        job = MaintenanceJob(scopes=frozenset({scope}))
    encoded = job.encode()
    assert isinstance(encoded, bytes)
    decoded = payload_cls.decode(encoded)
    assert decoded == job
    assert decoded.scopes == frozenset({scope})
    assert set(decoded.model_fields_set) == ({subject, "scopes"} if subject else {"scopes"})
    if subject:
        assert getattr(decoded, subject) == expected
