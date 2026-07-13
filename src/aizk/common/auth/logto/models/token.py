from typing import Annotated, Literal

from patos import FrozenModel
from pydantic.types import PositiveInt, StringConstraints


class Token(FrozenModel):
    """Management API client-credentials token."""

    access_token: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: PositiveInt = 3600
