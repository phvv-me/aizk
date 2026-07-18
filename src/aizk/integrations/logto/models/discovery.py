from patos import FrozenModel
from pydantic import Field
from pydantic.networks import AnyHttpUrl

from ....types import JWTAlgorithm


class Discovery(FrozenModel):
    """OIDC endpoints advertised by one Logto tenant."""

    issuer: AnyHttpUrl
    authorization_endpoint: AnyHttpUrl | None = None
    end_session_endpoint: AnyHttpUrl | None = None
    jwks_uri: AnyHttpUrl
    token_endpoint: AnyHttpUrl
    signing_algorithms: tuple[JWTAlgorithm, ...] = Field(
        validation_alias="id_token_signing_alg_values_supported"
    )
