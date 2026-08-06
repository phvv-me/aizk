from typing import Annotated

from patos import FrozenOpenModel
from pydantic import AliasChoices, Field
from pydantic.types import StringConstraints


class Account(FrozenOpenModel):
    """Directory-safe fields from one Logto user record."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    is_suspended: bool = Field(
        default=False,
        validation_alias=AliasChoices("isSuspended", "is_suspended"),
        serialization_alias="isSuspended",
    )
    username: str | None = None
    primary_email: str | None = Field(
        default=None,
        validation_alias=AliasChoices("primaryEmail", "primary_email"),
        serialization_alias="primaryEmail",
    )
    name: str | None = None
    avatar: str | None = None
