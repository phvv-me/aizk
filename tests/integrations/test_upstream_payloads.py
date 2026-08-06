# Every model over a third-party payload survives fields that payload was not asked for, which
# is the regression the outage earned. A house model forbids undeclared fields, which is right
# for a payload aizk writes and wrong for one it merely reads, because OIDC, the
# OpenAI-compatible APIs, Logto's Management API and Docling Serve all specify their responses
# as open and add metadata over time. Every payload below therefore carries the fields the real
# provider actually sends beyond the handful aizk consumes, so a model quietly moved back onto
# a strict base fails here rather than at the next deploy.

import pytest
from patos import FrozenModel, FrozenOpenModel
from pydantic import BaseModel, JsonValue, ValidationError

from aizk.integrations import logto as lt
from aizk.integrations.docling.models import DoclingDocument, DoclingResponse
from aizk.integrations.logto import policy as lt_policy
from aizk.ops.probes import ServedEntry, ServingIdentity
from aizk.serving.rerank.models import RerankRequest, RerankResponse, RerankResult

# One Logto OIDC discovery document. The four endpoints beyond `jwks_uri` and `token_endpoint`
# are the exact keys that took `logto-setup`, and with it `server`, `api` and `caddy`, down.
DISCOVERY: dict[str, JsonValue] = {
    "issuer": "https://auth.test/oidc",
    "authorization_endpoint": "https://auth.test/oidc/auth",
    "end_session_endpoint": "https://auth.test/oidc/session/end",
    "jwks_uri": "https://auth.test/oidc/jwks",
    "token_endpoint": "https://auth.test/oidc/token",
    "id_token_signing_alg_values_supported": ["ES384"],
    "introspection_endpoint": "https://auth.test/oidc/token/introspection",
    "revocation_endpoint": "https://auth.test/oidc/token/revocation",
    "backchannel_logout_supported": True,
    "backchannel_logout_session_supported": True,
    "claim_types_supported": ["normal"],
    "response_types_supported": ["code"],
    "subject_types_supported": ["public"],
    "userinfo_endpoint": "https://auth.test/oidc/me",
}

# One Logto user record. Everything past `avatar` is directory metadata aizk never reads.
ACCOUNT: dict[str, JsonValue] = {
    "id": "user-1",
    "username": "ada",
    "primaryEmail": "ada@test",
    "name": "Ada",
    "avatar": "https://auth.test/avatar.png",
    "isSuspended": False,
    "primaryPhone": None,
    "customData": {"seat": "annual"},
    "identities": {},
    "profile": {},
    "applicationId": "app-1",
    "hasPassword": True,
    "ssoIdentities": [],
    "lastSignInAt": 1_760_000_000_000,
    "createdAt": 1_750_000_000_000,
    "updatedAt": 1_750_000_000_000,
}

ORGANIZATION: dict[str, JsonValue] = {
    "id": "org-1",
    "name": "Toshiba",
    "description": "research",
    "customData": {"public": True},
    "isMfaRequired": False,
    "branding": {"logoUrl": "https://auth.test/logo.png"},
    "createdAt": 1_750_000_000_000,
}

ROLE: dict[str, JsonValue] = {
    "id": "role-1",
    "name": "admin",
    "description": "organization administrator",
    "type": "User",
    "isDefault": False,
    "tenantId": "tenant-1",
}

SCOPE: dict[str, JsonValue] = {
    "id": "scope-1",
    "name": "write:memory",
    "description": "write into the organization corpus",
    "tenantId": "tenant-1",
    "createdAt": 1_750_000_000_000,
}

# A verified Logto access token. `scope`, `client_id` and `jti` are on every token Logto mints,
# so a strict `Claims` rejects every authenticated caller rather than an unusual one.
CLAIMS: dict[str, JsonValue] = {
    "iss": "https://auth.test/oidc",
    "sub": "user-1",
    "aud": "https://aizk.test/mcp",
    "exp": 1_760_003_600,
    "iat": 1_760_000_000,
    "name": "Ada",
    "preferred_username": "ada",
    "username": "ada",
    "scope": "control",
    "client_id": "app-1",
    "jti": "token-1",
    "aizk_groups": ["org-1"],
}

# RFC 6749 lists `scope` in a client-credentials response and Logto returns it.
TOKEN: dict[str, JsonValue] = {
    "access_token": "m2m-token",
    "token_type": "Bearer",
    "expires_in": 3600,
    "scope": "all",
}

# Docling Serve answers with every output format it knows, holding `null` for the ones the
# request did not ask for, so `filename` and the four unrequested bodies always arrive.
DOCLING: dict[str, JsonValue] = {
    "document": {
        "filename": "paper.pdf",
        "md_content": "# Paper\n",
        "json_content": None,
        "html_content": None,
        "text_content": None,
        "doctags_content": None,
    },
    "status": "success",
    "processing_time": 0.25,
    "timings": {"pipeline": 0.2},
    "errors": [],
    "documents": [],
}

# vLLM answers `/rerank` with a full envelope, and each row repeats the scored document.
RERANK: dict[str, JsonValue] = {
    "id": "rerank-1",
    "model": "rerank-model",
    "object": "list",
    "usage": {"total_tokens": 128, "prompt_tokens": 128},
    "results": [
        {"index": 0, "relevance_score": 0.9, "document": {"text": "first"}},
        {"index": 1, "relevance_score": 0.4, "document": {"text": "second"}},
    ],
}

# An OpenAI-compatible `/models` listing, which is an envelope of entries that each carry
# ownership and permission metadata beside the identity the health probe reads.
SERVED_MODELS: dict[str, JsonValue] = {
    "object": "list",
    "data": [
        {
            "id": "extractor",
            "object": "model",
            "created": 1_760_000_000,
            "owned_by": "vllm",
            "root": "google/gemma-4-31B",
            "parent": None,
            "max_model_len": 3072,
            "permission": [{"id": "modelperm-1", "object": "model_permission"}],
        }
    ],
}


def test_discovery_reads_its_endpoints_out_of_a_full_oidc_document() -> None:
    """The document from the outage parses, and the endpoints aizk calls survive it."""
    discovery = lt.Discovery.model_validate(DISCOVERY)

    assert str(discovery.issuer) == DISCOVERY["issuer"]
    assert str(discovery.jwks_uri) == DISCOVERY["jwks_uri"]
    assert str(discovery.token_endpoint) == DISCOVERY["token_endpoint"]
    assert discovery.signing_algorithms == ("ES384",)


def test_account_reads_directory_fields_out_of_a_full_logto_user_record() -> None:
    """Identities, profile and timestamps are Logto's to send and aizk's to ignore."""
    account = lt.Account.model_validate(ACCOUNT)

    assert (account.id, account.username, account.name) == ("user-1", "ada", "Ada")
    assert account.primary_email == "ada@test"
    assert account.is_suspended is False


def test_organization_reads_its_public_flag_past_branding_and_mfa_metadata() -> None:
    """The public flag still decides scope visibility on a record carrying tenant settings."""
    organization = lt.Org.model_validate(
        {**ORGANIZATION, "organizationRoles": [ROLE], "scopes": [SCOPE]}
    )

    assert organization.name == "Toshiba"
    assert organization.is_public() is True
    assert organization.permits("write:memory") is True
    assert tuple(role.name for role in organization.roles) == ("admin",)


def test_member_reads_its_roles_out_of_a_full_organization_user_record() -> None:
    """An organization member arrives as a user record with roles bolted on."""
    member = lt.Member.model_validate({**ACCOUNT, "organizationRoles": [ROLE]})

    assert member.label == "Ada"
    assert tuple(role.name for role in member.roles) == ("admin",)


def test_role_and_scope_read_past_the_tenant_metadata_logto_attaches() -> None:
    """Both catalog records carry tenant bookkeeping aizk has no use for."""
    assert lt.Role.model_validate(ROLE).name == "admin"
    assert lt.OrganizationScope.model_validate(SCOPE).name == "write:memory"


def test_claims_resolve_an_identity_out_of_a_real_access_token() -> None:
    """Every Logto token carries scope, client and custom claims beside the standard ones."""
    claims = lt.Claims.model_validate(CLAIMS)

    assert (claims.sub, claims.name, claims.preferred_username) == ("user-1", "Ada", "ada")


def test_token_reads_the_credential_out_of_a_full_client_credentials_grant() -> None:
    """`scope` rides along on the grant, and rejecting it would stop every management call."""
    token = lt.Token.model_validate(TOKEN)

    assert (token.access_token, token.expires_in) == ("m2m-token", 3600)


@pytest.mark.parametrize(
    ("model", "payload", "field", "expected"),
    [
        (
            lt_policy._Resource,
            {**SCOPE, "indicator": "https://aizk.test", "accessTokenTtl": 3600},
            "indicator",
            "https://aizk.test",
        ),
        (lt_policy._Scope, SCOPE, "name", "write:memory"),
        (lt_policy._Role, ROLE, "is_default", False),
        (lt_policy._OrganizationRole, {**ROLE, "scopes": [SCOPE]}, "name", "admin"),
    ],
)
def test_policy_records_parse_a_full_management_api_page(
    model: type[FrozenOpenModel], payload: dict[str, JsonValue], field: str, expected: object
) -> None:
    """Policy reads whole Management API pages, where every row carries tenant bookkeeping."""
    assert getattr(model.model_validate(payload), field) == expected


def test_docling_response_parses_every_output_format_the_converter_reports() -> None:
    """Docling always names the file and always lists the bodies the request did not want."""
    response = DoclingResponse.model_validate(DOCLING)

    assert response.status == "success"
    assert response.document.md_content == "# Paper\n"
    assert response.markdown == "# Paper\n"


def test_rerank_response_parses_the_full_vllm_envelope() -> None:
    """Usage accounting and the echoed document sit beside the scores the ranker reads."""
    response = RerankResponse.model_validate(RERANK)

    assert [(row.index, row.relevance_score) for row in response.results] == [(0, 0.9), (1, 0.4)]


def test_serving_identity_reads_a_full_openai_model_listing() -> None:
    """The health probe names the served model out of an entry full of vendor metadata."""
    identity = ServingIdentity.decode(SERVED_MODELS)
    health = identity.endpoint_health("llm", "http://x/v1", True, "extractor")

    assert (health.model, health.served_as, health.matched) == (
        "google/gemma-4-31B",
        "extractor",
        True,
    )
    assert health.context_tokens == 3072


def test_serving_identity_finds_the_configured_model_among_many() -> None:
    """A hosted router lists thousands of models, so entry zero says nothing about health."""
    listing: dict[str, JsonValue] = {
        "object": "list",
        "data": [
            {"id": "meta/muse-spark-1.2", "object": "model"},
            {"id": "deepseek/deepseek-v4-flash-0731", "object": "model", "max_model_len": 1048576},
            {"id": "anthropic/claude-x", "object": "model"},
        ],
    }
    health = ServingIdentity.decode(listing).endpoint_health(
        "llm", "https://openrouter.ai/api/v1", True, "deepseek/deepseek-v4-flash-0731"
    )

    assert (health.served_as, health.matched) == ("deepseek/deepseek-v4-flash-0731", True)
    assert health.context_tokens == 1048576


def test_serving_identity_reports_a_real_mismatch_when_the_model_is_absent() -> None:
    """Falling back to the first entry keeps a genuinely misconfigured lane visible."""
    listing: dict[str, JsonValue] = {
        "object": "list",
        "data": [{"id": "meta/muse-spark-1.2", "object": "model"}],
    }
    health = ServingIdentity.decode(listing).endpoint_health("llm", "http://x/v1", True, "absent")

    assert (health.served_as, health.matched) == ("meta/muse-spark-1.2", False)


@pytest.mark.parametrize(
    "model",
    [
        lt.Account,
        lt.Claims,
        lt.Discovery,
        lt.Member,
        lt.Org,
        lt.OrganizationScope,
        lt.Role,
        lt.Token,
        lt_policy._OrganizationRole,
        lt_policy._Resource,
        lt_policy._Role,
        lt_policy._Scope,
        DoclingDocument,
        DoclingResponse,
        RerankResponse,
        RerankResult,
        ServedEntry,
        ServingIdentity,
    ],
)
def test_every_third_party_payload_model_sits_on_the_open_base(model: type[BaseModel]) -> None:
    """One base carries the decision, so no integration model can drift back to strict alone."""
    assert issubclass(model, FrozenOpenModel)
    assert model.model_config["extra"] == "ignore"


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (RerankRequest, {"model": "m", "query": "q", "documents": [], "truncation": "left"}),
        (lt_policy.PolicyReport, {"clean": True, "changed": ()}),
    ],
)
def test_payloads_aizk_authors_still_reject_a_field_it_never_declared(
    model: type[FrozenModel], payload: dict[str, JsonValue]
) -> None:
    """Opening the read side never opens the write side, where a stray key is a typo."""
    assert model.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(payload)
