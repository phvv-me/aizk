import os
import re
from dataclasses import dataclass

_DIGEST = re.compile(r"[0-9a-f]{64}")


def enabled(value: str) -> bool:
    """Parse one explicit environment switch."""
    return value.strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class DeploymentConfig:
    """Values that shape one isolated alpha stack without carrying secrets."""

    name: str = "aizk-cockroachdb"
    deploy_compute: bool = False
    image_digest: str = ""
    public_url: str = "https://aizk.phvv.me"
    logto_url: str = "https://replace.logto.app"
    logto_client_id: str = "replace-logto-management-client"
    oauth_client_id: str = "replace-logto-mcp-client"
    billing_email: str = ""
    emergency_budget_usd: int = 10
    database_url_parameter: str = "/aizk/cockroachdb/database-url"
    admin_database_url_parameter: str = "/aizk/cockroachdb/admin-database-url"
    openrouter_key_parameter: str = "/aizk/openrouter/api-key"
    logto_client_secret_parameter: str = "/aizk/logto/management-client-secret"
    oauth_client_secret_parameter: str = "/aizk/logto/mcp-client-secret"

    def __post_init__(self) -> None:
        if self.deploy_compute and _DIGEST.fullmatch(self.image_digest) is None:
            raise ValueError("compute deployment requires a lowercase 64 character image digest")
        if self.deploy_compute and any(
            "replace" in value
            for value in (
                self.public_url,
                self.logto_url,
                self.logto_client_id,
                self.oauth_client_id,
            )
        ):
            raise ValueError("compute deployment requires real public and Logto configuration")

    @classmethod
    def from_environment(cls) -> DeploymentConfig:
        """Load non-secret deployment inputs from `AIZK_AWS_` variables."""
        get = os.environ.get
        return cls(
            name=get("AIZK_AWS_NAME", cls.name),
            deploy_compute=enabled(get("AIZK_AWS_DEPLOY_COMPUTE", "false")),
            image_digest=get("AIZK_AWS_IMAGE_DIGEST", ""),
            public_url=get("AIZK_AWS_PUBLIC_URL", cls.public_url),
            logto_url=get("AIZK_AWS_LOGTO_URL", cls.logto_url),
            logto_client_id=get("AIZK_AWS_LOGTO_CLIENT_ID", cls.logto_client_id),
            oauth_client_id=get("AIZK_AWS_OAUTH_CLIENT_ID", cls.oauth_client_id),
            billing_email=get("AIZK_AWS_BILLING_EMAIL", ""),
            emergency_budget_usd=int(get("AIZK_AWS_EMERGENCY_BUDGET_USD", "10")),
            database_url_parameter=get(
                "AIZK_AWS_DATABASE_URL_PARAMETER", cls.database_url_parameter
            ),
            admin_database_url_parameter=get(
                "AIZK_AWS_ADMIN_DATABASE_URL_PARAMETER", cls.admin_database_url_parameter
            ),
            openrouter_key_parameter=get(
                "AIZK_AWS_OPENROUTER_KEY_PARAMETER", cls.openrouter_key_parameter
            ),
            logto_client_secret_parameter=get(
                "AIZK_AWS_LOGTO_CLIENT_SECRET_PARAMETER",
                cls.logto_client_secret_parameter,
            ),
            oauth_client_secret_parameter=get(
                "AIZK_AWS_OAUTH_CLIENT_SECRET_PARAMETER",
                cls.oauth_client_secret_parameter,
            ),
        )
