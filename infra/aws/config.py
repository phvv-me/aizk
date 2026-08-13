import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_DIGEST = re.compile(r"[0-9a-f]{64}")


def enabled(value: str) -> bool:
    """Parse one explicit environment switch."""
    return value.strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class DeploymentConfig:
    """Values that shape one isolated staging stack without carrying secrets."""

    name: str = "craizk-staging"
    region: str = "ap-southeast-1"
    deploy_compute: bool = False
    image_digest: str = ""
    public_url: str | None = None
    logto_url: str | None = None
    logto_client_id: str = ""
    spa_client_id: str = "rdxzy3laahdnyp1mgxndf"
    billing_email: str = ""
    monthly_budget_usd: int = 10
    db_null_pool: bool = False
    recall_access_recording_enabled: bool = False
    recall_communities_enabled: bool = True
    recall_entity_catalog_enabled: bool = True
    recall_graph_expansion_enabled: bool = True
    recall_profiles_enabled: bool = False
    recall_raptor_enabled: bool = False
    recall_sources_first: bool = True
    database_url_parameter: str = "/craizk/staging/database-url"
    admin_database_url_parameter: str = "/craizk/staging/admin-database-url"
    openrouter_key_parameter: str = "/craizk/staging/openrouter-api-key"
    logto_client_secret_parameter: str = "/craizk/staging/logto-management-client-secret"

    def __post_init__(self) -> None:
        if self.deploy_compute and _DIGEST.fullmatch(self.image_digest) is None:
            raise ValueError("compute deployment requires a lowercase 64 character image digest")
        if self.monthly_budget_usd <= 0:
            raise ValueError("monthly budget must be positive")
        if self.public_url is not None and urlsplit(self.public_url).path not in {"", "/"}:
            raise ValueError("public_url must be an origin without a path")
        if self.logto_enabled and not all(
            (
                self.public_url,
                self.logto_url,
                self.logto_client_id,
                self.spa_client_id,
            )
        ):
            raise ValueError("Logto deployment requires every public and client setting")

    @property
    def logto_enabled(self) -> bool:
        """Report whether the public application authentication boundary is configured."""
        return any(
            (
                self.public_url,
                self.logto_url,
                self.logto_client_id,
            )
        )

    @classmethod
    def from_environment(cls) -> DeploymentConfig:
        """Load non-secret deployment inputs from `AIZK_AWS_` variables."""
        get = os.environ.get
        return cls(
            name=get("AIZK_AWS_NAME", cls.name),
            region=get("AIZK_AWS_REGION", cls.region),
            deploy_compute=enabled(get("AIZK_AWS_DEPLOY_COMPUTE", "false")),
            image_digest=get("AIZK_AWS_IMAGE_DIGEST", ""),
            public_url=get("AIZK_AWS_PUBLIC_URL") or None,
            logto_url=get("AIZK_AWS_LOGTO_URL") or None,
            logto_client_id=get("AIZK_AWS_LOGTO_CLIENT_ID", ""),
            spa_client_id=get("AIZK_AWS_SPA_CLIENT_ID", cls.spa_client_id),
            billing_email=get("AIZK_AWS_BILLING_EMAIL", ""),
            monthly_budget_usd=int(get("AIZK_AWS_MONTHLY_BUDGET_USD", "10")),
            db_null_pool=enabled(get("AIZK_AWS_DB_NULL_POOL", "false")),
            recall_access_recording_enabled=enabled(
                get("AIZK_AWS_RECALL_ACCESS_RECORDING_ENABLED", "false")
            ),
            recall_communities_enabled=enabled(get("AIZK_AWS_RECALL_COMMUNITIES_ENABLED", "true")),
            recall_entity_catalog_enabled=enabled(
                get("AIZK_AWS_RECALL_ENTITY_CATALOG_ENABLED", "true")
            ),
            recall_graph_expansion_enabled=enabled(
                get("AIZK_AWS_RECALL_GRAPH_EXPANSION_ENABLED", "true")
            ),
            recall_profiles_enabled=enabled(get("AIZK_AWS_RECALL_PROFILES_ENABLED", "false")),
            recall_raptor_enabled=enabled(get("AIZK_AWS_RECALL_RAPTOR_ENABLED", "false")),
            recall_sources_first=enabled(get("AIZK_AWS_RECALL_SOURCES_FIRST", "true")),
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
        )
