import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import fire
import httpx
from patos import FrozenOpenModel
from pydantic import Field, SecretStr

from aizk.integrations.logto.models import Token

_required_redirects = frozenset(
    {
        "http://localhost:8912/callback",
        "http://127.0.0.1:8912/callback",
        "http://127.0.0.1:8912/callback/TT1XHO0Hgg3N",
    }
)


class OidcClientMetadata(FrozenOpenModel):
    """Redirect boundary returned for one Logto application."""

    redirect_uris: tuple[str, ...] = Field(validation_alias="redirectUris")


class Application(FrozenOpenModel):
    """Logto application fields needed by the deployment guard."""

    name: str
    type: Literal["Native", "MachineToMachine", "SPA", "Traditional", "Protected", "SAML"]
    oidc_client_metadata: OidcClientMetadata = Field(validation_alias="oidcClientMetadata")


class OAuthClient(FrozenOpenModel):
    """Public OAuth identity bundled with one MCP connection."""

    client_id: str = Field(validation_alias="clientId")


class McpServer(FrozenOpenModel):
    """Bundled MCP connection whose public client Logto must accept."""

    oauth: OAuthClient


class ClaudeMcpConfig(FrozenOpenModel):
    """Claude plugin MCP configuration used by the public setup."""

    mcp_servers: dict[str, McpServer] = Field(validation_alias="mcpServers")


@dataclass(frozen=True, slots=True)
class LogtoDeploymentGuard:
    """Refuse deployment when interactive OAuth crosses the management boundary."""

    endpoint: str
    management_client_id: str
    management_client_secret: SecretStr
    public_client_id: str
    deployment_client_id: str | None = None
    transport: httpx.BaseTransport | None = None

    @classmethod
    def from_environment(cls) -> LogtoDeploymentGuard:
        """Load the tenant authority and canonical public client without exposing a secret."""
        project = Path(__file__).parents[2]
        plugin = ClaudeMcpConfig.model_validate_json(
            (project / "plugins/aizk/claude.mcp.json").read_text()
        )
        return cls(
            endpoint=os.environ["CRAIZK_LOGTO_URL"].rstrip("/"),
            management_client_id=os.environ["CRAIZK_LOGTO_MANAGEMENT_CLIENT_ID"],
            management_client_secret=SecretStr(
                os.environ["CRAIZK_LOGTO_MANAGEMENT_CLIENT_SECRET"]
            ),
            public_client_id=plugin.mcp_servers["aizk"].oauth.client_id,
            deployment_client_id=os.environ.get("AIZK_AWS_LOGTO_PUBLIC_CLIENT_ID"),
        )

    def verify(self) -> dict[str, str | list[str]]:
        """Verify distinct client types and every exact loopback redirect required by agents."""
        if self.public_client_id == self.management_client_id:
            raise ValueError("public OAuth client must differ from the Management API client")
        if (
            self.deployment_client_id is not None
            and self.deployment_client_id != self.public_client_id
        ):
            raise ValueError("deployment OAuth client must match the bundled agent client")

        with httpx.Client(timeout=20, transport=self.transport) as client:
            token = Token.model_validate(
                client.post(
                    f"{self.endpoint}/oidc/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.management_client_id,
                        "client_secret": self.management_client_secret.get_secret_value(),
                        "resource": f"{self.endpoint}/api",
                        "scope": "all",
                    },
                )
                .raise_for_status()
                .json()
            )
            headers = {"authorization": f"Bearer {token.access_token}"}
            management = Application.model_validate(
                client.get(
                    f"{self.endpoint}/api/applications/{self.management_client_id}",
                    headers=headers,
                )
                .raise_for_status()
                .json()
            )
            public = Application.model_validate(
                client.get(
                    f"{self.endpoint}/api/applications/{self.public_client_id}",
                    headers=headers,
                )
                .raise_for_status()
                .json()
            )

        if management.type != "MachineToMachine":
            raise ValueError("Management API client must remain MachineToMachine")
        if public.type != "Native":
            raise ValueError("public OAuth client must remain Native")
        missing = _required_redirects.difference(public.oidc_client_metadata.redirect_uris)
        if missing:
            raise ValueError(f"public OAuth client is missing redirects {sorted(missing)}")

        return {
            "management_client": management.type,
            "public_client": public.type,
            "redirects": sorted(_required_redirects),
        }


class LogtoCli:
    """Expose the fail-closed Logto deployment check to deployment automation."""

    def verify(self) -> dict[str, str | list[str]]:
        """Verify the live tenant against the bundled public client."""
        return LogtoDeploymentGuard.from_environment().verify()


if __name__ == "__main__":
    fire.Fire(LogtoCli)
