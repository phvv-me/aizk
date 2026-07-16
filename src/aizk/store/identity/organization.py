from functools import cached_property

from patos import FrozenModel
from pydantic import UUID5, Field
from pydantic.types import JsonValue


class OrganizationMember(FrozenModel):
    """One Logto organization member with only directory-safe identity fields."""

    name: str | None = None
    username: str | None = None
    avatar: str | None = None
    roles: tuple[str, ...] = ()

    @cached_property
    def label(self) -> str:
        """Return the best human-readable member identifier supplied by Logto."""
        return self.name or self.username or "unnamed member"


class OrganizationStanding(FrozenModel):
    """One Logto organization with its directory and the caller's effective standing."""

    id: UUID5 = Field(exclude=True)
    name: str
    description: str | None = None
    custom_data: dict[str, JsonValue] = Field(default_factory=dict)
    members: tuple[OrganizationMember, ...] = ()
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    public: bool = False
    writable: bool = False

    @cached_property
    def members_by_name(self) -> dict[str, OrganizationMember]:
        """Index members by their best available Logto display label."""
        return {member.label: member for member in self.members}
