from typing import Annotated

from patos import FrozenModel
from pydantic import AliasChoices, Field
from pydantic.types import JsonValue, StringConstraints

from .role import Role


class Org(FrozenModel):
    """Organization and current member roles returned by Logto."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    custom_data: dict[str, JsonValue] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("customData", "custom_data"),
        serialization_alias="customData",
    )
    roles: tuple[Role, ...] = Field(
        default=(),
        validation_alias=AliasChoices("organizationRoles", "organization_roles"),
        serialization_alias="organizationRoles",
    )

    def is_public(self) -> bool:
        """Whether Logto marks this organization as publicly readable."""
        return self.custom_data.get("public") is True

    def is_writable(self, writable_roles: frozenset[str]) -> bool:
        """Whether this member has any role configured as writable by Aizk."""
        return any(role.name in writable_roles for role in self.roles)
