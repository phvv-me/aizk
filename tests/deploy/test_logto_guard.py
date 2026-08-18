from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from infra.aws.logto import LogtoDeploymentGuard

_management_id = "management-client"
_public_id = "public-client"
_redirects = [
    "http://localhost:8912/callback",
    "http://127.0.0.1:8912/callback",
    "http://127.0.0.1:8912/callback/TT1XHO0Hgg3N",
]


def transport(
    *, public_type: str = "Native", redirects: list[str] | None = None
) -> httpx.MockTransport:
    """Return one Logto tenant with controllable public application metadata."""
    public_redirects = _redirects if redirects is None else redirects

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/token":
            return httpx.Response(
                200,
                json={"access_token": "management-token", "token_type": "Bearer"},
            )
        application_type = (
            "MachineToMachine" if request.url.path.endswith(_management_id) else public_type
        )
        application_redirects = [] if application_type == "MachineToMachine" else public_redirects
        return httpx.Response(
            200,
            json={
                "name": f"{application_type} application",
                "type": application_type,
                "oidcClientMetadata": {"redirectUris": application_redirects},
            },
        )

    return httpx.MockTransport(handle)


def guard(mock_transport: httpx.MockTransport) -> LogtoDeploymentGuard:
    """Build the deployment guard without carrying live tenant credentials."""
    return LogtoDeploymentGuard(
        endpoint="https://tenant.logto.app",
        management_client_id=_management_id,
        management_client_secret=SecretStr("secret"),
        public_client_id=_public_id,
        transport=mock_transport,
    )


def test_logto_guard_accepts_distinct_clients_and_exact_redirects() -> None:
    assert guard(transport()).verify() == {
        "management_client": "MachineToMachine",
        "public_client": "Native",
        "redirects": sorted(_redirects),
    }


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: guard(transport(public_type="MachineToMachine")), "must remain Native"),
        (lambda: guard(transport(redirects=_redirects[:-1])), "is missing redirects"),
    ],
)
def test_logto_guard_rejects_an_unsafe_public_client(
    build: Callable[[], LogtoDeploymentGuard], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        build().verify()


def test_logto_guard_rejects_management_client_reuse_before_network() -> None:
    reused = LogtoDeploymentGuard(
        endpoint="https://tenant.logto.app",
        management_client_id=_management_id,
        management_client_secret=SecretStr("secret"),
        public_client_id=_management_id,
    )

    with pytest.raises(ValueError, match="must differ"):
        reused.verify()


def test_logto_guard_rejects_deployment_client_drift_before_network() -> None:
    drifted = LogtoDeploymentGuard(
        endpoint="https://tenant.logto.app",
        management_client_id=_management_id,
        management_client_secret=SecretStr("secret"),
        public_client_id=_public_id,
        deployment_client_id="older-client",
    )

    with pytest.raises(ValueError, match="must match"):
        drifted.verify()
