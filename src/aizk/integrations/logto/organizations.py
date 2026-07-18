from typing import Self
from urllib.parse import quote

import httpx
from patos import FrozenFlexModel, FrozenModel
from pydantic import model_validator

from ...config import settings
from ...store.identity import User
from .client import LogtoClient
from .models import Member, Org, Role


class OrganizationChange(FrozenModel):
    """One completed Logto organization mutation safe to show in the web UI."""

    organization: str
    member: str | None = None


class OrganizationManager(FrozenFlexModel):
    """Mediate narrow organization actions over the server-held Logto M2M credential."""

    client: LogtoClient
    user: User
    subject: str

    @model_validator(mode="after")
    def require_current_user(self) -> Self:
        """Admit only the authenticated identity matching the raw Logto subject."""
        if self.user.is_anonymous() or self.user.id != settings.subject_id(self.subject):
            raise PermissionError("organization administration requires a current user")
        return self

    async def create(self, name: str, description: str | None = None) -> OrganizationChange:
        """Create one private organization and make its authenticated creator an admin."""
        normalized = name.strip()
        if not normalized:
            raise ValueError("Organization name is required.")
        if any(
            item.name.casefold() == normalized.casefold()
            for item in await self.client.organizations(fresh=True)
        ):
            raise ValueError("That organization name is already in use.")
        creator_role = await self.role(settings.logto_creator_role)
        response = await self.client.management(
            "POST",
            "api/organizations",
            payload={
                "name": normalized,
                "description": description.strip()
                if description and description.strip()
                else None,
                "customData": {"public": False},
            },
        )
        organization = Org(**response.json())
        try:
            await self.client.management(
                "POST",
                f"api/organizations/{quote(organization.id, safe='')}/users",
                payload={"userIds": [self.subject]},
            )
            await self.assign_role(organization.id, self.subject, creator_role)
        except httpx.HTTPError:
            await self.client.management(
                "DELETE", f"api/organizations/{quote(organization.id, safe='')}"
            )
            raise
        self.client.caches.invalidate(
            self.subject,
            organization_ids=(organization.id,),
        )
        return OrganizationChange(organization=organization.name)

    async def add(self, organization_name: str, email: str, role_name: str) -> OrganizationChange:
        """Add one existing exact-email account without exposing Logto's global directory."""
        organization = await self.organization(organization_name, "manage:member")
        normalized_email = email.strip().casefold()
        if "@" not in normalized_email:
            raise ValueError("Enter the account's exact email address.")
        account = await self.client.account_by_email(normalized_email)
        if account is None:
            raise ValueError(
                "Could not add that account. Ask the person to create an AIZK account first."
            )
        if any(member.id == account.id for member in organization.members):
            raise ValueError("That account is already a member of this organization.")
        role = await self.role(role_name)
        path = f"api/organizations/{quote(organization.id, safe='')}/users"
        await self.client.management("POST", path, payload={"userIds": [account.id]})
        try:
            await self.assign_role(organization.id, account.id, role)
        except httpx.HTTPError:
            await self.client.management(
                "DELETE",
                f"{path}/{quote(account.id, safe='')}",
            )
            raise
        self.client.caches.invalidate(
            *self.roster(organization, account.id),
            organization_ids=(organization.id,),
        )
        return OrganizationChange(organization=organization.name, member=normalized_email)

    async def set_role(
        self, organization_name: str, member_id: str, role_name: str
    ) -> OrganizationChange:
        """Replace one current member's role after checking live manage permission."""
        organization = await self.organization(organization_name, "manage:member")
        member = self.member(organization, member_id)
        role = await self.role(role_name)
        self.ensure_admin_survives(organization, member, role)
        await self.assign_role(organization.id, member.id, role, replace=True)
        self.client.caches.invalidate(
            *self.roster(organization, member.id),
            organization_ids=(organization.id,),
        )
        return OrganizationChange(organization=organization.name, member=member.label)

    async def remove(self, organization_name: str, member_id: str) -> OrganizationChange:
        """Remove one current member after checking live delete permission."""
        organization = await self.organization(organization_name, "delete:member")
        member = self.member(organization, member_id)
        if member.id == self.subject:
            raise ValueError("Transfer administration before removing your own membership.")
        self.ensure_admin_survives(organization, member)
        await self.client.management(
            "DELETE",
            (
                f"api/organizations/{quote(organization.id, safe='')}/users/"
                f"{quote(member.id, safe='')}"
            ),
        )
        self.client.caches.invalidate(
            *self.roster(organization, member.id),
            organization_ids=(organization.id,),
        )
        return OrganizationChange(organization=organization.name, member=member.label)

    def roster(self, organization: Org, *affected: str) -> tuple[str, ...]:
        """Every subject whose cached authority snapshot embeds this organization.

        Invalidating only the mutated organization would leave the other members'
        materialized `user_orgs` snapshots stale until TTL, so a mutation cascades to
        the acting subject, the directly affected accounts, and the whole roster.
        """
        return tuple(
            dict.fromkeys(
                (self.subject, *affected, *(member.id for member in organization.members))
            )
        )

    async def organization(self, name: str, permission: str) -> Org:
        """Return one current member organization only when Logto grants the action."""
        organization = next(
            (
                item
                for item in await self.client.user_orgs(self.subject, fresh=True)
                if item.name == name
            ),
            None,
        )
        if organization is None or not organization.permits(permission):
            raise PermissionError("Organization administration is not permitted.")
        return organization

    async def role(self, name: str) -> Role:
        """Resolve one configured user-facing role from Logto's live template."""
        if name not in settings.logto_role_permissions:
            raise ValueError("Select a supported organization role.")
        role = next(
            (
                item
                for item in await self.client.organization_roles(fresh=True)
                if item.name == name
            ),
            None,
        )
        if role is None:
            raise RuntimeError(f"Logto has no configured {name} organization role")
        return role

    async def assign_role(
        self, organization_id: str, member_id: str, role: Role, replace: bool = False
    ) -> None:
        """Assign or replace a member role through Logto's role endpoint."""
        await self.client.management(
            "PUT" if replace else "POST",
            (
                f"api/organizations/{quote(organization_id, safe='')}/users/"
                f"{quote(member_id, safe='')}/roles"
            ),
            payload={"organizationRoleIds": [role.id]},
        )

    @staticmethod
    def member(organization: Org, member_id: str) -> Member:
        """Resolve a member only from the already authorized organization directory."""
        member = next((item for item in organization.members if item.id == member_id), None)
        if member is None:
            raise ValueError("That account is not a current organization member.")
        return member

    @staticmethod
    def ensure_admin_survives(
        organization: Org,
        member: Member,
        replacement: Role | None = None,
    ) -> None:
        """Reject a mutation that would leave the organization without an administrator."""
        is_admin = any(role.name == settings.logto_creator_role for role in member.roles)
        remains_admin = replacement is not None and replacement.name == settings.logto_creator_role
        administrators = sum(
            any(role.name == settings.logto_creator_role for role in item.roles)
            for item in organization.members
        )
        if is_admin and not remains_admin and administrators == 1:
            raise ValueError("Assign another administrator before changing the final admin.")
