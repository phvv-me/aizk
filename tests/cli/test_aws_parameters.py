import json
import os

import pytest

import aizk.config.parameters as parameter_module
from aizk.config.parameters import ParameterResponse, SsmEnvironment


class ParameterClient:
    """Record the one SSM read made by the cold-start loader."""

    def __init__(self, response: ParameterResponse) -> None:
        self.response = response
        self.calls: list[tuple[list[str], bool]] = []

    def get_parameters(
        self,
        *,
        Names: list[str],
        WithDecryption: bool,
    ) -> ParameterResponse:
        self.calls.append((Names, WithDecryption))
        return self.response


def test_ssm_environment_loads_one_decrypted_parameter_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ParameterClient(
        {
            "Parameters": [
                {"Name": "/demo/database", "Value": "secret-database"},
                {"Name": "/demo/model", "Value": "secret-model"},
            ]
        }
    )
    loader = SsmEnvironment(
        {
            "AIZK_DATABASE_URL": "/demo/database",
            "AIZK_EMBED_API_KEY": "/demo/model",
            "AIZK_LLM_API_KEY": "/demo/model",
        },
        client,
    )
    for name in loader.variables:
        monkeypatch.delenv(name, raising=False)

    loader.load()

    assert client.calls == [(["/demo/database", "/demo/model"], True)]
    assert loader.variables == json.loads(
        '{"AIZK_DATABASE_URL":"/demo/database",'
        '"AIZK_EMBED_API_KEY":"/demo/model",'
        '"AIZK_LLM_API_KEY":"/demo/model"}'
    )
    assert os.environ["AIZK_DATABASE_URL"] == "secret-database"
    assert os.environ["AIZK_EMBED_API_KEY"] == "secret-model"
    assert os.environ["AIZK_LLM_API_KEY"] == "secret-model"


def test_configured_ssm_environment_validates_the_lambda_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ParameterClient({"Parameters": []})
    monkeypatch.setenv(
        "AIZK_AWS_PARAMETER_ENV",
        '{"AIZK_DATABASE_URL":"/demo/database"}',
    )
    monkeypatch.setattr(parameter_module.boto3, "client", lambda service: client)

    configured = SsmEnvironment.configured()

    assert configured is not None
    assert configured.variables == {"AIZK_DATABASE_URL": "/demo/database"}
    assert configured.client is client


def test_parameter_environment_entrypoint_loads_the_configured_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ParameterClient(
        {"Parameters": [{"Name": "/demo/database", "Value": "secret-database"}]}
    )
    loader = SsmEnvironment({"AIZK_DATABASE_URL": "/demo/database"}, client)
    monkeypatch.delenv("AIZK_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        SsmEnvironment,
        "configured",
        classmethod(lambda cls: loader),
    )

    parameter_module.load_parameter_environment()

    assert os.environ["AIZK_DATABASE_URL"] == "secret-database"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"InvalidParameters": ["/missing"]}, "unavailable SSM parameters"),
        ({"Parameters": []}, "missing SSM parameters"),
    ],
)
def test_ssm_environment_fails_closed_on_incomplete_reads(
    response: ParameterResponse,
    message: str,
) -> None:
    loader = SsmEnvironment({"AIZK_DATABASE_URL": "/missing"}, ParameterClient(response))

    with pytest.raises(RuntimeError, match=message):
        loader.load()
