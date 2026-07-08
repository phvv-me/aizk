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
    """Each queue payload decodes back to exactly the ids its worker body reads, unchanged.

    The chunk and profile jobs carry their own subject plus the user, and the task job carries
    only the user, so a round trip through `encode`/`decode` reproduces the fields as built,
    the wire-format contract a durable job depends on across enqueue and dequeue.
    """
    job: ChunkJob | ProfileJob | TaskJob
    if payload_cls is ChunkJob:
        job = ChunkJob(chunk_id=first, user_id=second)
    elif payload_cls is ProfileJob:
        job = ProfileJob(entity_id=first, user_id=second)
    else:
        job = TaskJob(user_id=second)
    encoded = job.encode()
    assert isinstance(encoded, bytes)
    decoded = payload_cls.decode(encoded)
    assert decoded == job
    assert decoded.user_id == second
    if subject:
        assert getattr(decoded, subject) == first
