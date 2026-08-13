from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import dbutil
import pytest
from id_factory import uuid5, uuid8
from obstore.store import MemoryStore

import aizk.mcp.runtime as runtime_module
from aizk.artifacts.service import ArtifactIntake
from aizk.config import Settings
from aizk.integrations.docling import ArtifactBytes
from aizk.mcp.runtime import McpRuntime, TextOnlyArtifacts
from aizk.storage import ByteStore
from aizk.store import Blob


def test_text_only_artifacts_reject_every_file_operation() -> None:
    artifacts = TextOnlyArtifacts()
    user = dbutil.actor(uuid5())
    payload = ArtifactBytes(
        content=b"plain text",
        filename="note.txt",
        media_type="text/plain",
    )
    target = frozenset({user.id})

    with pytest.raises(RuntimeError, match="accepts text memories only"):
        dbutil.run(artifacts.uri(user, "https://example.test/note.txt"))
    with pytest.raises(RuntimeError, match="accepts text memories only"):
        dbutil.run(artifacts.accept(user, payload, target=target))
    with pytest.raises(RuntimeError, match="no artifact byte store"):
        dbutil.run(
            artifacts.get(
                "objects/missing",
                encoding=Blob.Encoding.identity,
                expected_size=len(payload.content),
                expected_hash=uuid8(),
                version="1",
            )
        )


def test_runtime_assembles_the_text_only_profile() -> None:
    config = Settings(_env_file=None, artifact_ingest_enabled=False)

    runtime = McpRuntime.assemble(config)

    assert runtime.settings is config
    assert isinstance(runtime.artifacts, TextOnlyArtifacts)
    assert runtime.uploads.intake is runtime.artifacts
    assert runtime.store is None
    assert runtime.artifact_store is runtime.artifacts


def test_runtime_assembles_the_artifact_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Settings(_env_file=None)
    monkeypatch.setattr(config, "artifact_ingest_enabled", True)
    store = ByteStore(
        backend=MemoryStore(),
        upload_byte_limit=1024,
        internal_download_lifetime=timedelta(minutes=5),
    )
    intake = ArtifactIntake(
        reader=MagicMock(),
        scanner=MagicMock(),
        storage=store,
        repository=MagicMock(),
        enqueuer=MagicMock(),
    )
    monkeypatch.setattr(runtime_module, "build_byte_store", lambda settings: store)
    monkeypatch.setattr(
        runtime_module,
        "build_artifact_services",
        lambda settings, storage: SimpleNamespace(intake=intake),
    )

    runtime = McpRuntime.assemble(config)

    assert runtime.artifacts is intake
    assert runtime.uploads.intake is intake
    assert runtime.store is store
    assert runtime.artifact_store is store


def test_runtime_rejects_artifact_intake_without_its_store() -> None:
    config = Settings(_env_file=None)
    store = ByteStore(
        backend=MemoryStore(),
        upload_byte_limit=1024,
        internal_download_lifetime=timedelta(minutes=5),
    )
    intake = ArtifactIntake(
        reader=MagicMock(),
        scanner=MagicMock(),
        storage=store,
        repository=MagicMock(),
        enqueuer=MagicMock(),
    )
    runtime = McpRuntime(
        settings=config,
        artifacts=intake,
        uploads=MagicMock(),
        auth=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="requires a byte store"):
        _ = runtime.artifact_store
