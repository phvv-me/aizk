from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock

import dbutil
import httpx
from bg_doubles import fake_artifact_services, fake_runtime
from id_factory import uuid5

import aizk.runtime as runtime_module
from aizk.artifacts.configured import ArtifactServices
from aizk.artifacts.service import ArtifactIntake, ArtifactIntegrity
from aizk.background.jobs.conversion import DoclingConversionJob
from aizk.integrations.logto import LogtoClient
from aizk.memory import Memory
from aizk.store.identity import OrganizationStanding, User


def test_artifact_and_runtime_contexts_close_every_owned_client(
    monkeypatch,
) -> None:
    http = httpx.AsyncClient()
    artifacts = ArtifactServices(
        intake=cast("ArtifactIntake", None),
        conversion=cast("DoclingConversionJob", None),
        integrity=cast("ArtifactIntegrity", None),
        http_clients=(http,),
    )
    close_logto = AsyncMock()
    close_models = AsyncMock()
    runtime = replace(
        fake_runtime(artifacts=artifacts),
        logto=cast("LogtoClient", type("LogtoDouble", (), {"close": close_logto})()),
    )
    monkeypatch.setattr(runtime_module, "close_clients", close_models)

    async def use_runtime() -> None:
        async with runtime as entered:
            assert entered is runtime

    dbutil.run(use_runtime())

    assert http.is_closed
    close_logto.assert_awaited_once_with()
    close_models.assert_awaited_once_with()


def test_memory_status_and_user_serialization_are_directory_safe() -> None:
    organization = OrganizationStanding(id=uuid5(), name="Research")
    user = User.authorized(
        uuid5(),
        read=(organization.id,),
        organizations=(organization,),
        name="Pedro",
        username="pedro",
    )

    assert Memory(user, fake_artifact_services().intake).status is user
    serialized = user.model_dump(mode="json")
    assert serialized["name"] == "Pedro"
    assert serialized["username"] == "pedro"
    assert serialized["organizations"][0]["name"] == "Research"
    assert "id" not in serialized
    assert "scopes" not in serialized
