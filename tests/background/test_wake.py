from typing import cast
from unittest.mock import MagicMock

import dbutil
import pytest
from botocore.exceptions import ClientError
from mypy_boto3_lambda import LambdaClient

import aizk.background.wake as wake_module
from aizk.background.wake import LambdaWorkerWake, NoopWorkerWake, configured_worker_wake
from aizk.config import Settings


def test_noop_worker_wake_completes_locally() -> None:
    dbutil.run(NoopWorkerWake().wake())


def test_lambda_worker_wake_invokes_asynchronously() -> None:
    client = MagicMock()
    wake = LambdaWorkerWake("aizk-worker", cast("LambdaClient", client))

    dbutil.run(wake.wake())

    client.invoke.assert_called_once_with(
        FunctionName="aizk-worker",
        InvocationType="Event",
        Payload=b"{}",
    )


def test_lambda_worker_wake_leaves_failures_to_scheduled_recovery() -> None:
    client = MagicMock()
    client.invoke.side_effect = ClientError(
        {"Error": {"Code": "Throttled", "Message": "later"}},
        "Invoke",
    )
    wake = LambdaWorkerWake("aizk-worker", cast("LambdaClient", client))

    dbutil.run(wake.wake())

    client.invoke.assert_called_once()


def test_configured_worker_wake_selects_the_named_lambda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    monkeypatch.setattr(wake_module.boto3, "client", lambda service: client)

    local = configured_worker_wake(Settings())
    cloud = configured_worker_wake(Settings(worker_function_name="aizk-worker"))

    assert isinstance(local, NoopWorkerWake)
    assert isinstance(cloud, LambdaWorkerWake)
    assert cloud.function_name == "aizk-worker"
