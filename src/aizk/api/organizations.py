from ..integrations.logto import LogtoClient
from ..integrations.logto.models import Org
from .dashboard import View


class OrganizationMemberView(View):
    """One member identifier disclosed only inside an authorized organization directory."""

    id: str
    label: str
    roles: tuple[str, ...] = ()


class OrganizationView(View):
    """One current membership and its exact live administration capabilities."""

    name: str
    description: str = ""
    roles: tuple[str, ...] = ()
    members: tuple[OrganizationMemberView, ...] = ()
    can_manage_members: bool = False
    can_delete_members: bool = False

    @classmethod
    def from_org(cls, organization: Org) -> OrganizationView:
        """Hide member identifiers unless Logto grants a member-management permission."""
        can_manage = organization.permits("manage:member")
        can_delete = organization.permits("delete:member")
        return cls(
            name=organization.name,
            description=organization.description or "",
            roles=tuple(role.name for role in organization.roles),
            members=(
                tuple(
                    OrganizationMemberView(
                        id=member.id,
                        label=member.label,
                        roles=tuple(role.name for role in member.roles),
                    )
                    for member in organization.members
                )
                if can_manage or can_delete
                else ()
            ),
            can_manage_members=can_manage,
            can_delete_members=can_delete,
        )


class OrganizationDirectory(View):
    """Current caller memberships loaded from Logto without a tenant-wide user listing."""

    organizations: tuple[OrganizationView, ...] = ()

    @classmethod
    async def load(cls, client: LogtoClient, subject: str) -> OrganizationDirectory:
        """Load only the signed-in subject's organizations and permission-filtered members."""
        return cls(
            organizations=tuple(
                OrganizationView.from_org(item)
                for item in sorted(
                    await client.user_orgs(subject, fresh=True),
                    key=lambda item: item.name.casefold(),
                )
            )
        )
