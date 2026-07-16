from typing import Annotated

from patos import FrozenModel
from pydantic import AliasChoices, Field
from pydantic.types import JsonValue, StringConstraints

from .member import Member
from .role import Role
from .scope import OrganizationScope


class Org(FrozenModel):
    """Organization and current member roles returned by Logto."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    description: str | None = None
    custom_data: dict[str, JsonValue] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("customData", "custom_data"),
        serialization_alias="customData",
    )
    members: tuple[Member, ...] = ()
    roles: tuple[Role, ...] = Field(
        default=(),
        validation_alias=AliasChoices("organizationRoles", "organization_roles"),
        serialization_alias="organizationRoles",
    )
    scopes: tuple[OrganizationScope, ...] = ()

    def is_public(self) -> bool:
        """Whether Logto marks this organization as publicly readable."""
        return self.custom_data.get("public") is True

    def permits(self, permission: str) -> bool:
        """Whether Logto grants this member the named organization permission."""
        return any(scope.name == permission for scope in self.scopes)
