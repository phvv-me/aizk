import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aizk.background.payloads import ChunkJob, ProfileJob, TaskJob

uuids = st.uuids()


@pytest.mark.parametrize(
    ("payload_cls", "subject"),
    [(ChunkJob, "chunk_id"), (ProfileJob, "entity_id"), (TaskJob, None)],
    ids=["chunk", "profile", "task"],
)
@given(first=uuids, second=uuids)
def test_payload_round_trips_exactly_the_fields_the_worker_decodes(
    payload_cls: type[ChunkJob | ProfileJob | TaskJob],
    subject: str | None,
    first: uuid.UUID,
    second: uuid.UUID,
) -> None:
    job: ChunkJob | ProfileJob | TaskJob
    if payload_cls is ChunkJob:
        job = ChunkJob(chunk_id=first, scopes=frozenset({second}))
    elif payload_cls is ProfileJob:
        job = ProfileJob(entity_id=first, scopes=frozenset({second}))
    else:
        job = TaskJob(scopes=frozenset({second}))
    encoded = job.encode()
    assert isinstance(encoded, bytes)
    decoded = payload_cls.decode(encoded)
    assert decoded == job
    assert decoded.scopes == frozenset({second})
    if subject:
        assert getattr(decoded, subject) == first
