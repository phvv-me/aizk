import hashlib
from uuid import NAMESPACE_URL, uuid5

import dbutil
import pytest

from aizk.artifacts.models import ArtifactReceipt
from aizk.artifacts.uploads import UploadBox, UploadRequest, UploadTicket
from aizk.integrations.docling import ArtifactBytes
from aizk.store.identity import User
from aizk.types import Scopes


class _RecordingIntake:
    def __init__(self) -> None:
        self.artifacts: list[ArtifactBytes] = []
        self.receipt = ArtifactReceipt.model_construct()

    async def accept(
        self,
        user: User,
        artifact: ArtifactBytes,
        *,
        target: Scopes,
        companion_text: str | None = None,
    ) -> ArtifactReceipt:
        self.artifacts.append(artifact)
        return self.receipt


def _ticket(content: bytes) -> UploadTicket:
    user_id = uuid5(NAMESPACE_URL, "https://aizk.example/test-user")
    return UploadTicket(
        user=User.model_construct(id=user_id),
        declared=UploadRequest(
            filename="evidence.txt",
            media_type="text/plain",
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        ),
        target=frozenset({user_id}),
    )


def test_deliver_rejects_hash_mismatch_before_artifact_intake() -> None:
    intake = _RecordingIntake()
    uploads = UploadBox(intake=intake)

    with pytest.raises(ValueError, match="the upload does not match its declared content hash"):
        dbutil.run(uploads.deliver(_ticket(b"abc"), b"abz"))

    assert intake.artifacts == []


def test_deliver_accepts_matching_content_hash() -> None:
    intake = _RecordingIntake()
    uploads = UploadBox(intake=intake)
    content = b"hash-bound evidence"

    receipt = dbutil.run(uploads.deliver(_ticket(content), content))

    assert receipt is intake.receipt
    assert len(intake.artifacts) == 1
    assert intake.artifacts[0].content == content
