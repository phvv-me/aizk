from functools import cached_property
from typing import Annotated

from patos import FrozenModel
from pydantic import AliasChoices, Field
from pydantic.types import StringConstraints

from .role import Role


class Member(FrozenModel):
    """Directory-safe fields from one Logto organization member record."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    username: str | None = None
    name: str | None = None
    avatar: str | None = None
    roles: tuple[Role, ...] = Field(
        default=(),
        validation_alias=AliasChoices("organizationRoles", "organization_roles"),
        serialization_alias="organizationRoles",
    )

    @cached_property
    def label(self) -> str:
        """Return the best directory label available within this organization."""
        return self.name or self.username or "unnamed member"
