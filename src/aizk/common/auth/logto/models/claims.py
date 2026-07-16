from typing import Annotated

from patos import FrozenModel
from pydantic.networks import AnyHttpUrl
from pydantic.types import PositiveInt, StringConstraints


class Claims(FrozenModel):
    """Verified standard claims from a Logto access or ID token."""

    iss: AnyHttpUrl
    sub: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    aud: str | tuple[str, ...]
    exp: PositiveInt
    iat: PositiveInt
    name: str | None = None
    preferred_username: str | None = None
    username: str | None = None
