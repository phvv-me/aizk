from typing import Annotated

from patos import FrozenModel
from pydantic.types import StringConstraints


class Account(FrozenModel):
    """Directory-safe fields from one Logto user record."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    username: str | None = None
    name: str | None = None
    avatar: str | None = None
