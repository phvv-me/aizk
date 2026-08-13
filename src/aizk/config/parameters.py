import os
from collections.abc import Mapping
from typing import Protocol, TypedDict, cast

import boto3
from pydantic import TypeAdapter

_PARAMETER_ENVIRONMENT = "AIZK_AWS_PARAMETER_ENV"


class ParameterValue(TypedDict):
    """Decrypted SSM parameter fields used by the cold-start loader."""

    Name: str
    Value: str


class ParameterResponse(TypedDict, total=False):
    """Relevant shape returned by the SSM batch read."""

    Parameters: list[ParameterValue]
    InvalidParameters: list[str]


class ParameterClient(Protocol):
    """Small SSM client surface needed during Lambda cold start."""

    def get_parameters(
        self,
        *,
        Names: list[str],
        WithDecryption: bool,
    ) -> ParameterResponse: ...


class SsmEnvironment:
    """Resolve named SecureString values into process settings before AIZK creates config."""

    def __init__(
        self, variables: Mapping[str, str], client: ParameterClient | None = None
    ) -> None:
        self.variables = dict(variables)
        self.client = client or cast(ParameterClient, boto3.client("ssm"))

    @classmethod
    def configured(cls) -> SsmEnvironment | None:
        """Build the loader only when an AWS deployment supplies parameter names."""
        encoded = os.environ.get(_PARAMETER_ENVIRONMENT)
        if encoded is None:
            return None
        variables = TypeAdapter(dict[str, str]).validate_json(encoded)
        return cls(variables)

    def load(self) -> None:
        """Fetch one decrypted batch and populate the exact configured environment names."""
        names = sorted(set(self.variables.values()))
        response = self.client.get_parameters(Names=names, WithDecryption=True)
        invalid = response.get("InvalidParameters", [])
        if invalid:
            raise RuntimeError("unavailable SSM parameters " + ", ".join(sorted(invalid)))
        values = {
            parameter["Name"]: parameter["Value"] for parameter in response.get("Parameters", [])
        }
        missing = set(names) - values.keys()
        if missing:
            raise RuntimeError("missing SSM parameters " + ", ".join(sorted(missing)))
        os.environ.update(
            {environment_name: values[name] for environment_name, name in self.variables.items()}
        )


def load_parameter_environment() -> None:
    """Load configured Lambda secrets before creating the global AIZK settings."""
    loader = SsmEnvironment.configured()
    if loader is not None:
        loader.load()
