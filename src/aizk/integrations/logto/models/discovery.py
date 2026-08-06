from patos import FrozenOpenModel
from pydantic import Field
from pydantic.networks import AnyHttpUrl

from ....types import JWTAlgorithm


# A discovery document belongs to the identity provider, not to aizk, and OIDC is explicit
# that a provider may advertise more metadata than any one reader consumes. The house models
# forbid unknown fields because our own payloads are exactly what we declare, but applying
# that rule to somebody else's document turns every upstream addition into a hard startup
# failure. Logto adding token introspection and back-channel logout metadata is a correct
# thing for Logto to do, so the named endpoints below are read and the rest is ignored.
class Discovery(FrozenOpenModel):
    """OIDC endpoints advertised by one Logto tenant."""

    issuer: AnyHttpUrl
    authorization_endpoint: AnyHttpUrl | None = None
    end_session_endpoint: AnyHttpUrl | None = None
    jwks_uri: AnyHttpUrl
    token_endpoint: AnyHttpUrl
    signing_algorithms: tuple[JWTAlgorithm, ...] = Field(
        validation_alias="id_token_signing_alg_values_supported"
    )
