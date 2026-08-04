from typing import Annotated, Literal, cast
from urllib.parse import quote

from patos import FrozenModel
from pydantic import AliasChoices, Field, TypeAdapter
from pydantic.types import JsonValue, PositiveInt, StringConstraints

from ...config import settings
from .client import LogtoClient
from .models import Account

type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class PolicyReport(FrozenModel):
    """Result of auditing or applying the configured Logto authorization policy."""

    clean: bool
    changes: tuple[str, ...] = ()


class RoleAssignment(FrozenModel):
    """One global role under the managed prefix and the accounts Logto assigns to it."""

    name: str
    description: str | None = None
    default: bool = False
    managed: bool = True
    members: tuple[Account, ...] = ()


class RoleReport(FrozenModel):
    """Every global role under the managed prefix with its current assignments."""

    roles: tuple[RoleAssignment, ...] = ()


class _Scope(FrozenModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    description: str | None = None


class _Resource(FrozenModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    indicator: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    access_token_ttl: PositiveInt = Field(
        validation_alias=AliasChoices("accessTokenTtl", "access_token_ttl")
    )


class _Role(FrozenModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    description: str | None = None
    type: Literal["User", "MachineToMachine"] = "User"
    is_default: bool = Field(
        default=False,
        validation_alias=AliasChoices("isDefault", "is_default"),
    )


class _OrganizationRole(FrozenModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    description: str | None = None
    type: Literal["User", "MachineToMachine"] = "User"
    scopes: tuple[_Scope, ...] = ()


class _Change(FrozenModel):
    method: HttpMethod
    path: str
    message: str
    payload: dict[str, JsonValue] | None = None


class _Managed(FrozenModel):
    """One global role AIZK owns, named as the operator reads it in a change message."""

    name: str
    description: str
    label: str
    default: bool


_RESOURCES = TypeAdapter(tuple[_Resource, ...])
_SCOPES = TypeAdapter(tuple[_Scope, ...])
_ROLES = TypeAdapter(tuple[_Role, ...])
_ORGANIZATION_ROLES = TypeAdapter(tuple[_OrganizationRole, ...])
_ACCOUNTS = TypeAdapter(tuple[Account, ...])


def _managed_roles() -> tuple[_Managed, ...]:
    """The two global roles policy owns, the default one for people and the operator one."""
    return (
        _Managed(
            name=settings.logto_user_role,
            description=settings.logto_user_role_description,
            label="default user role",
            default=True,
        ),
        _Managed(
            name=settings.logto_admin_role,
            description=settings.logto_admin_role_description,
            label="operator role",
            default=False,
        ),
    )


class LogtoPolicy:
    """Audit and reconcile the Logto resources that define AIZK authorization.

    Only the configured API resource, its required scopes, global roles under the managed
    prefix, named organization roles, and their configured organization permissions are
    changed. Unrelated Logto resources and permissions remain untouched.
    """

    def __init__(self, client: LogtoClient) -> None:
        self.client = client

    async def audit(self) -> PolicyReport:
        """Report every Management API mutation needed to reach the configured policy."""
        changes = await self._plan()
        return PolicyReport(clean=not changes, changes=tuple(change.message for change in changes))

    async def apply(self) -> PolicyReport:
        """Apply policy drift until a fresh audit is clean and return the mutations made."""
        applied: list[str] = []
        for _ in range(8):
            changes = await self._plan()
            if not changes:
                return PolicyReport(clean=True, changes=tuple(applied))
            try:
                for change in changes:
                    await self.client.management(
                        change.method,
                        change.path,
                        payload=change.payload,
                    )
                    applied.append(change.message)
            finally:
                # Mutations reshape roles and permissions tenant-wide, so every cached
                # authority snapshot must die with them rather than outlive a revocation.
                self.client.caches.invalidate_all()
        raise RuntimeError("Logto authorization policy did not converge after eight passes")

    async def roles(self) -> RoleReport:
        """Report every global role under the managed prefix with the accounts assigned to it.

        A role the configuration no longer names is reported as unmanaged, which is exactly
        what the next `apply` deletes.
        """
        owned = {spec.name for spec in _managed_roles()}
        found = [
            role
            for role in await self.client.pages("api/roles", _ROLES)
            if role.name.startswith(settings.logto_managed_role_prefix)
        ]
        assignments = [
            RoleAssignment(
                name=role.name,
                description=role.description,
                default=role.is_default,
                managed=role.name in owned,
                members=await self.client.pages(
                    f"api/roles/{quote(role.id, safe='')}/users",
                    _ACCOUNTS,
                ),
            )
            for role in sorted(found, key=lambda role: role.name)
        ]
        return RoleReport(roles=tuple(assignments))

    async def _plan(self) -> tuple[_Change, ...]:
        """Build the next dependency-safe set of idempotent Management API mutations."""
        changes: list[_Change] = []
        resource = await self._resource(changes)
        if resource is None:
            return tuple(changes)
        required_scope_ids = await self._resource_scopes(resource, changes)
        if required_scope_ids is None:
            return tuple(changes)
        if not await self._global_roles(required_scope_ids, changes):
            return tuple(changes)
        organization_scopes = await self._organization_scopes(changes)
        if organization_scopes is None:
            return tuple(changes)
        await self._organization_roles(organization_scopes, changes)
        self._retired_organization_scopes(organization_scopes, changes)
        return tuple(changes)

    async def _resource(self, changes: list[_Change]) -> _Resource | None:
        """Find the AIZK API resource and plan its creation or current mutable fields."""
        resource = next(
            (
                item
                for item in await self.client.pages("api/resources", _RESOURCES)
                if item.indicator == settings.mcp_resource_id
            ),
            None,
        )
        if resource is None:
            changes.append(
                _Change(
                    method="POST",
                    path="api/resources",
                    message=f"create API resource {settings.mcp_resource_id}",
                    payload={
                        "name": settings.logto_api_name,
                        "indicator": settings.mcp_resource_id,
                        "accessTokenTtl": settings.logto_api_token_seconds,
                    },
                )
            )
            return None
        if (
            resource.name != settings.logto_api_name
            or resource.access_token_ttl != settings.logto_api_token_seconds
        ):
            changes.append(
                _Change(
                    method="PATCH",
                    path=f"api/resources/{quote(resource.id, safe='')}",
                    message=f"update API resource {settings.mcp_resource_id}",
                    payload={
                        "name": settings.logto_api_name,
                        "accessTokenTtl": settings.logto_api_token_seconds,
                    },
                )
            )
        return resource

    async def _resource_scopes(
        self, resource: _Resource, changes: list[_Change]
    ) -> list[str] | None:
        """Plan missing API permissions and return their IDs once all exist."""
        resource_scopes = await self.client.pages(
            f"api/resources/{quote(resource.id, safe='')}/scopes",
            _SCOPES,
        )
        scopes_by_name = {scope.name: scope for scope in resource_scopes}
        missing_scopes = settings.logto_required_scopes - scopes_by_name.keys()
        changes.extend(
            _Change(
                method="POST",
                path=f"api/resources/{quote(resource.id, safe='')}/scopes",
                message=f"create API permission {name}",
                payload={
                    "name": name,
                    "description": settings.logto_scope_descriptions[name],
                },
            )
            for name in sorted(missing_scopes)
        )
        if missing_scopes:
            return None
        return [scopes_by_name[name].id for name in sorted(settings.logto_required_scopes)]

    async def _global_roles(self, required_scope_ids: list[str], changes: list[_Change]) -> bool:
        """Plan every owned global role, its API permissions, and obsolete managed roles.

        Both the default user role and the operator role carry the same required API
        permissions, so a token minted for either verifies against the AIZK resource.
        """
        roles = await self.client.pages("api/roles", _ROLES)
        by_name = {role.name: role for role in roles}
        managed = _managed_roles()
        ready = [
            await self._managed_role(by_name.get(spec.name), spec, required_scope_ids, changes)
            for spec in managed
        ]
        if not all(ready):
            return False
        owned = {spec.name for spec in managed}
        changes.extend(
            _Change(
                method="DELETE",
                path=f"api/roles/{quote(role.id, safe='')}",
                message=f"delete obsolete managed role {role.name}",
            )
            for role in roles
            if role.type == "User"
            and role.name.startswith(settings.logto_managed_role_prefix)
            and role.name not in owned
        )
        return True

    async def _managed_role(
        self,
        role: _Role | None,
        spec: _Managed,
        required_scope_ids: list[str],
        changes: list[_Change],
    ) -> bool:
        """Plan one owned global role and its required API permissions."""
        if role is not None and role.type != "User":
            changes.append(
                _Change(
                    method="DELETE",
                    path=f"api/roles/{quote(role.id, safe='')}",
                    message=f"replace non-user role {spec.name}",
                )
            )
            return False
        if role is None:
            changes.append(
                _Change(
                    method="POST",
                    path="api/roles",
                    message=f"create {spec.label} {spec.name}",
                    payload=cast(
                        "dict[str, JsonValue]",
                        {
                            "name": spec.name,
                            "description": spec.description,
                            "type": "User",
                            "isDefault": spec.default,
                            "scopeIds": required_scope_ids,
                        },
                    ),
                )
            )
        else:
            changes.extend(await self._managed_role_drift(role, spec, required_scope_ids))
        return True

    async def _managed_role_drift(
        self, role: _Role, spec: _Managed, required_scope_ids: list[str]
    ) -> tuple[_Change, ...]:
        """Plan description, default flag, and permission drift on one existing owned role."""
        changes: list[_Change] = []
        if role.description != spec.description or role.is_default != spec.default:
            changes.append(
                _Change(
                    method="PATCH",
                    path=f"api/roles/{quote(role.id, safe='')}",
                    message=f"update {spec.label} {spec.name}",
                    payload={"description": spec.description, "isDefault": spec.default},
                )
            )
        assigned = await self.client.pages(
            f"api/roles/{quote(role.id, safe='')}/scopes",
            _SCOPES,
        )
        assigned_ids = {scope.id for scope in assigned}
        missing_ids = [scope_id for scope_id in required_scope_ids if scope_id not in assigned_ids]
        if missing_ids:
            changes.append(
                _Change(
                    method="POST",
                    path=f"api/roles/{quote(role.id, safe='')}/scopes",
                    message=f"grant API permissions to {spec.name}",
                    payload=cast("dict[str, JsonValue]", {"scopeIds": missing_ids}),
                )
            )
        return tuple(changes)

    async def _organization_scopes(self, changes: list[_Change]) -> dict[str, _Scope] | None:
        """Find every managed organization permission or plan missing entries."""
        organization_scopes = await self.client.pages("api/organization-scopes", _SCOPES)
        scopes_by_name = {scope.name: scope for scope in organization_scopes}
        missing = settings.logto_organization_permissions.keys() - scopes_by_name.keys()
        changes.extend(
            _Change(
                method="POST",
                path="api/organization-scopes",
                message=f"create organization permission {name}",
                payload={"name": name, "description": description},
            )
            for name, description in settings.logto_organization_permissions.items()
            if name in missing
        )
        changes.extend(
            _Change(
                method="PATCH",
                path=f"api/organization-scopes/{quote(scopes_by_name[name].id, safe='')}",
                message=f"update organization permission {name}",
                payload={"description": description},
            )
            for name, description in settings.logto_organization_permissions.items()
            if name not in missing and scopes_by_name[name].description != description
        )
        return None if missing else scopes_by_name

    async def _organization_roles(self, scopes: dict[str, _Scope], changes: list[_Change]) -> None:
        """Plan configured organization roles and their exact managed permissions."""
        organization_roles = await self.client.pages(
            "api/organization-roles",
            _ORGANIZATION_ROLES,
        )
        roles_by_name = {role.name: role for role in organization_roles}
        for name, description in settings.logto_organization_roles.items():
            changes.extend(
                self._organization_role(
                    roles_by_name.get(name),
                    name,
                    description,
                    scopes,
                )
            )

    @staticmethod
    def _retired_organization_scopes(
        scopes: dict[str, _Scope],
        changes: list[_Change],
    ) -> None:
        """Delete permissions whose corresponding AIZK feature no longer exists."""
        changes.extend(
            _Change(
                method="DELETE",
                path=f"api/organization-scopes/{quote(scopes[name].id, safe='')}",
                message=f"delete retired organization permission {name}",
            )
            for name in sorted(settings.logto_retired_organization_permissions & scopes.keys())
        )

    @staticmethod
    def _organization_role(
        role: _OrganizationRole | None,
        name: str,
        description: str,
        scopes: dict[str, _Scope],
    ) -> tuple[_Change, ...]:
        """Plan one role while preserving permissions AIZK does not manage."""
        wanted = settings.logto_role_permissions[name]
        wanted_ids = [scopes[permission].id for permission in sorted(wanted)]
        if role is None:
            return (
                _Change(
                    method="POST",
                    path="api/organization-roles",
                    message=f"create organization role {name}",
                    payload=cast(
                        "dict[str, JsonValue]",
                        {
                            "name": name,
                            "description": description,
                            "type": "User",
                            "organizationScopeIds": wanted_ids,
                            "resourceScopeIds": [],
                        },
                    ),
                ),
            )
        changes: list[_Change] = []
        if role.description != description or role.type != "User":
            changes.append(
                _Change(
                    method="PATCH",
                    path=f"api/organization-roles/{quote(role.id, safe='')}",
                    message=f"update organization role {name}",
                    payload={"description": description, "type": "User"},
                )
            )
        changes.extend(LogtoPolicy._organization_role_scopes(role, name, wanted, scopes))
        return tuple(changes)

    @staticmethod
    def _organization_role_scopes(
        role: _OrganizationRole,
        name: str,
        wanted: frozenset[str],
        scopes: dict[str, _Scope],
    ) -> tuple[_Change, ...]:
        """Plan grants for missing managed permissions and revokes for unwanted ones."""
        changes: list[_Change] = []
        assigned = {scope.name: scope for scope in role.scopes}
        missing = wanted - assigned.keys()
        if missing:
            changes.append(
                _Change(
                    method="POST",
                    path=f"api/organization-roles/{quote(role.id, safe='')}/scopes",
                    message=f"grant organization permissions to {name}",
                    payload={
                        "organizationScopeIds": [
                            scopes[permission].id for permission in sorted(missing)
                        ]
                    },
                )
            )
        managed = (
            settings.logto_organization_permissions.keys()
            | settings.logto_retired_organization_permissions
        )
        unwanted = (assigned.keys() & managed) - wanted
        changes.extend(
            _Change(
                method="DELETE",
                path=(
                    f"api/organization-roles/{quote(role.id, safe='')}/scopes/"
                    f"{quote(assigned[permission].id, safe='')}"
                ),
                message=f"revoke {permission} from {name}",
            )
            for permission in sorted(unwanted)
        )
        return tuple(changes)
