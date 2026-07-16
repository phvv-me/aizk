import pytest
from hypothesis import given
from id_factory import uuid5s, uuid7s
from pydantic import UUID5, UUID7

from aizk.background.jobs.models import ChunkJob, MaintenanceJob


@pytest.mark.parametrize(
    ("payload_cls", "subject"),
    [(ChunkJob, "chunk_id"), (MaintenanceJob, None)],
    ids=["chunk", "task"],
)
@given(identity=uuid5s, record=uuid7s, scope=uuid5s)
def test_payload_round_trips_exactly_the_fields_the_worker_decodes(
    payload_cls: type[ChunkJob | MaintenanceJob],
    subject: str | None,
    identity: UUID5,
    record: UUID7,
    scope: UUID5,
) -> None:
    job: ChunkJob | MaintenanceJob
    if payload_cls is ChunkJob:
        expected: UUID5 | UUID7 = record
        job = ChunkJob(chunk_id=record, scopes=frozenset({scope}))
    else:
        expected = identity
        job = MaintenanceJob(scopes=frozenset({scope}))
    encoded = job.encode()
    assert isinstance(encoded, bytes)
    decoded = payload_cls.decode(encoded)
    assert decoded == job
    assert decoded.scopes == frozenset({scope})
    if subject:
        assert getattr(decoded, subject) == expected
