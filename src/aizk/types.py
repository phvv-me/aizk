from typing import Annotated, Literal

from pydantic import UUID5, StringConstraints

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
