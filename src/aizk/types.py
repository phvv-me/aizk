from typing import Annotated, Literal

from pydantic import UUID5, StringConstraints, WithJsonSchema
from pydantic import UUID7 as PydanticUUID7

type JWTAlgorithm = Literal[
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
    "RS256",
    "RS384",
    "RS512",
]
type Scopes = frozenset[UUID5]
type ScopeName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type ScopeNames = list[ScopeName]

# UUID7 on the wire, spelled with the standard `uuid` JSON Schema format so MCP
# clients whose validators only know RFC 4122 formats accept the tool schemas.
type UUID7 = Annotated[PydanticUUID7, WithJsonSchema({"type": "string", "format": "uuid"})]
