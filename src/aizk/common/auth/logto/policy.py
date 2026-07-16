from typing import Annotated, Literal
from urllib.parse import quote

from patos import FrozenModel
from pydantic import AliasChoices, Field, TypeAdapter
from pydantic.types import JsonValue, PositiveInt, StringConstraints

from ....config import settings
from .client import LogtoClient

type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class PolicyReport(FrozenModel):
    """Result of auditing or applying the configured Logto authorization policy."""

    clean: bool
    changes: tuple[str, ...] = ()


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


_RESOURCES = TypeAdapter(tuple[_Resource, ...])
_SCOPES = TypeAdapter(tuple[_Scope, ...])
_ROLES = TypeAdapter(tuple[_Role, ...])
_ORGANIZATION_ROLES = TypeAdapter(tuple[_OrganizationRole, ...])


class LogtoPolicy:
    """Audit and reconcile the Logto resources that define AIZK authorization.

    Only the configured API resource, its required scopes, global roles under the managed
    prefix, the named organization roles, and the configured organization write permission
    are changed. Unrelated Logto resources and permissions remain untouched.
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
            for change in changes:
                await self.client.management(
                    change.method,
                    change.path,
                    payload=change.payload,
                )
                applied.append(change.message)
        raise RuntimeError("Logto authorization policy did not converge after eight passes")

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
        write_scope = await self._organization_scope(changes)
        if write_scope is None:
            return tuple(changes)
        await self._organization_roles(write_scope, changes)
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
        """Plan the default user role, its API permissions, and obsolete managed roles."""
        roles = await self.client.pages("api/roles", _ROLES)
        user_role = next((role for role in roles if role.name == settings.logto_user_role), None)
        if not await self._user_role(user_role, required_scope_ids, changes):
            return False
        changes.extend(
            _Change(
                method="DELETE",
                path=f"api/roles/{quote(role.id, safe='')}",
                message=f"delete obsolete managed role {role.name}",
            )
            for role in roles
            if role.type == "User"
            and role.name.startswith(settings.logto_managed_role_prefix)
            and role.name != settings.logto_user_role
        )
        return True

    async def _user_role(
        self,
        user_role: _Role | None,
        required_scope_ids: list[str],
        changes: list[_Change],
    ) -> bool:
        """Plan the one default global human role and its required API permissions."""
        if user_role is not None and user_role.type != "User":
            changes.append(
                _Change(
                    method="DELETE",
                    path=f"api/roles/{quote(user_role.id, safe='')}",
                    message=f"replace non-user role {settings.logto_user_role}",
                )
            )
            return False
        if user_role is None:
            changes.append(
                _Change(
                    method="POST",
                    path="api/roles",
                    message=f"create default user role {settings.logto_user_role}",
                    payload={
                        "name": settings.logto_user_role,
                        "description": settings.logto_user_role_description,
                        "type": "User",
                        "isDefault": True,
                        "scopeIds": required_scope_ids,
                    },
                )
            )
        else:
            if (
                user_role.description != settings.logto_user_role_description
                or not user_role.is_default
            ):
                changes.append(
                    _Change(
                        method="PATCH",
                        path=f"api/roles/{quote(user_role.id, safe='')}",
                        message=f"update default user role {settings.logto_user_role}",
                        payload={
                            "description": settings.logto_user_role_description,
                            "isDefault": True,
                        },
                    )
                )
            assigned = await self.client.pages(
                f"api/roles/{quote(user_role.id, safe='')}/scopes",
                _SCOPES,
            )
            assigned_ids = {scope.id for scope in assigned}
            missing_ids = [
                scope_id for scope_id in required_scope_ids if scope_id not in assigned_ids
            ]
            if missing_ids:
                changes.append(
                    _Change(
                        method="POST",
                        path=f"api/roles/{quote(user_role.id, safe='')}/scopes",
                        message=f"grant API permissions to {settings.logto_user_role}",
                        payload={"scopeIds": missing_ids},
                    )
                )
        return True

    async def _organization_scope(self, changes: list[_Change]) -> _Scope | None:
        """Find the shared-write permission or plan its creation."""
        organization_scopes = await self.client.pages("api/organization-scopes", _SCOPES)
        write_scope = next(
            (
                scope
                for scope in organization_scopes
                if scope.name == settings.logto_write_permission
            ),
            None,
        )
        if write_scope is None:
            changes.append(
                _Change(
                    method="POST",
                    path="api/organization-scopes",
                    message=f"create organization permission {settings.logto_write_permission}",
                    payload={
                        "name": settings.logto_write_permission,
                        "description": settings.logto_write_permission_description,
                    },
                )
            )
            return None
        return write_scope

    async def _organization_roles(self, write_scope: _Scope, changes: list[_Change]) -> None:
        """Plan configured organization roles and their exact shared-write standing."""
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
                    write_scope,
                )
            )

    @staticmethod
    def _organization_role(
        role: _OrganizationRole | None,
        name: str,
        description: str,
        write_scope: _Scope,
    ) -> tuple[_Change, ...]:
        """Plan one organization role without changing its unrelated permissions."""
        writable = name in settings.logto_writable_roles
        if role is None:
            return (
                _Change(
                    method="POST",
                    path="api/organization-roles",
                    message=f"create organization role {name}",
                    payload={
                        "name": name,
                        "description": description,
                        "type": "User",
                        "organizationScopeIds": [write_scope.id] if writable else [],
                        "resourceScopeIds": [],
                    },
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
        has_write = any(scope.id == write_scope.id for scope in role.scopes)
        if writable and not has_write:
            changes.append(
                _Change(
                    method="POST",
                    path=f"api/organization-roles/{quote(role.id, safe='')}/scopes",
                    message=f"grant {settings.logto_write_permission} to {name}",
                    payload={"organizationScopeIds": [write_scope.id]},
                )
            )
        elif not writable and has_write:
            changes.append(
                _Change(
                    method="DELETE",
                    path=(
                        f"api/organization-roles/{quote(role.id, safe='')}/scopes/"
                        f"{quote(write_scope.id, safe='')}"
                    ),
                    message=f"revoke {settings.logto_write_permission} from {name}",
                )
            )
        return tuple(changes)
