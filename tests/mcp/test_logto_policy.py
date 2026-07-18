from types import SimpleNamespace
from typing import Literal, cast

import dbutil
import httpx
import pytest
from pydantic import AnyHttpUrl, TypeAdapter
from pydantic.types import JsonValue

from aizk.config import settings
from aizk.integrations.logto import LogtoClient, LogtoPolicy

type JsonObject = dict[str, JsonValue]
type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class LogtoState:
    def __init__(self) -> None:
        self.resources: list[JsonObject] = []
        self.resource_scopes: list[JsonObject] = []
        self.roles: list[JsonObject] = []
        self.role_scopes: dict[str, list[JsonObject]] = {}
        self.organization_scopes: list[JsonObject] = []
        self.organization_roles: list[JsonObject] = []

    @classmethod
    def clean(cls) -> LogtoState:
        state = cls()
        state.resources = [
            {
                "id": "resource-a",
                "name": settings.logto_api_name,
                "indicator": settings.mcp_resource_id,
                "accessTokenTtl": settings.logto_api_token_seconds,
            }
        ]
        state.resource_scopes = [
            {
                "id": "scope-control",
                "name": "control",
                "description": settings.logto_scope_descriptions["control"],
            }
        ]
        state.roles = [
            {
                "id": "role-user",
                "name": settings.logto_user_role,
                "description": settings.logto_user_role_description,
                "type": "User",
                "isDefault": True,
            },
            {
                "id": "role-m2m",
                "name": "Logto Management API access",
                "type": "MachineToMachine",
            },
        ]
        state.role_scopes = {"role-user": [state.resource_scopes[0]]}
        state.organization_scopes = [
            {
                "id": f"scope-{name.replace(':', '-')}",
                "name": name,
                "description": description,
            }
            for name, description in settings.logto_organization_permissions.items()
        ]
        state.organization_roles = [
            {
                "id": f"org-role-{name}",
                "name": name,
                "description": description,
                "type": "User",
                "scopes": [
                    scope
                    for scope in state.organization_scopes
                    if scope["name"] in settings.logto_role_permissions[name]
                ],
            }
            for name, description in settings.logto_organization_roles.items()
        ]
        return state


class FakeLogto:
    def __init__(self, state: LogtoState, *, mutable: bool = True) -> None:
        self.state = state
        self.mutable = mutable
        self.calls: list[tuple[HttpMethod, str, JsonObject | None]] = []
        self.invalidations = 0
        self.caches = SimpleNamespace(invalidate_all=self.invalidate_all)

    def invalidate_all(self) -> None:
        self.invalidations += 1

    async def pages[T](self, path: str, adapter: TypeAdapter[tuple[T, ...]]) -> tuple[T, ...]:
        if path == "api/resources":
            items = self.state.resources
        elif path.endswith("/scopes") and path.startswith("api/resources/"):
            items = self.state.resource_scopes
        elif path == "api/roles":
            items = self.state.roles
        elif path.endswith("/scopes") and path.startswith("api/roles/"):
            items = self.state.role_scopes.get(path.split("/")[2], [])
        elif path == "api/organization-scopes":
            items = self.state.organization_scopes
        else:
            assert path == "api/organization-roles"
            items = self.state.organization_roles
        return adapter.validate_python(items)

    async def management(
        self,
        method: HttpMethod,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        payload: JsonObject | None = None,
    ) -> httpx.Response:
        assert params is None
        self.calls.append((method, path, payload))
        if self.mutable:
            self.mutate(method, path, payload)
        return httpx.Response(204, request=httpx.Request(method, f"https://auth.test/{path}"))

    def mutate(self, method: HttpMethod, path: str, payload: JsonObject | None) -> None:
        body = payload or {}
        if path == "api/resources":
            self.state.resources.append({"id": "resource-a", **body})
        elif path == "api/resources/resource-a" and method == "PATCH":
            self.state.resources[0].update(body)
        elif path == "api/resources/resource-a/scopes":
            self.state.resource_scopes.append({"id": "scope-control", **body})
        elif path == "api/roles" and method == "POST":
            self.state.roles.append({"id": "role-user", **body})
            scope_ids = cast("list[str]", body["scopeIds"])
            self.state.role_scopes["role-user"] = [
                scope for scope in self.state.resource_scopes if scope["id"] in scope_ids
            ]
        elif path.startswith("api/roles/"):
            self.mutate_role(method, path, body)
        elif path == "api/organization-scopes":
            name = cast("str", body["name"])
            self.state.organization_scopes.append(
                {"id": f"scope-{name.replace(':', '-')}", **body}
            )
        elif path.startswith("api/organization-scopes/"):
            scope_id = path.rsplit("/", 1)[-1]
            if method == "DELETE":
                self.state.organization_scopes = [
                    scope for scope in self.state.organization_scopes if scope["id"] != scope_id
                ]
                for role in self.state.organization_roles:
                    role["scopes"] = [
                        scope
                        for scope in cast("list[JsonObject]", role["scopes"])
                        if scope["id"] != scope_id
                    ]
            else:
                next(
                    scope for scope in self.state.organization_scopes if scope["id"] == scope_id
                ).update(body)
        elif path == "api/organization-roles" and method == "POST":
            name = cast("str", body["name"])
            scope_ids = cast("list[str]", body["organizationScopeIds"])
            self.state.organization_roles.append(
                {
                    "id": f"org-role-{name}",
                    **body,
                    "scopes": [
                        scope
                        for scope in self.state.organization_scopes
                        if scope["id"] in scope_ids
                    ],
                }
            )
        else:
            self.mutate_organization_role(method, path, body)

    def mutate_role(self, method: HttpMethod, path: str, body: JsonObject) -> None:
        role_id = path.split("/")[2]
        if method == "DELETE":
            self.state.roles = [role for role in self.state.roles if role["id"] != role_id]
        elif path.endswith("/scopes"):
            scope_ids = cast("list[str]", body["scopeIds"])
            assigned = self.state.role_scopes.setdefault(role_id, [])
            assigned.extend(
                scope
                for scope in self.state.resource_scopes
                if scope["id"] in scope_ids and scope not in assigned
            )
        else:
            next(role for role in self.state.roles if role["id"] == role_id).update(body)

    def mutate_organization_role(self, method: HttpMethod, path: str, body: JsonObject) -> None:
        parts = path.split("/")
        role = next(item for item in self.state.organization_roles if item["id"] == parts[2])
        if method == "PATCH":
            role.update(body)
            return
        scopes = cast("list[JsonObject]", role["scopes"])
        if method == "POST":
            scope_ids = cast("list[str]", body["organizationScopeIds"])
            scopes.extend(
                scope
                for scope in self.state.organization_scopes
                if scope["id"] in scope_ids and scope not in scopes
            )
        else:
            role["scopes"] = [scope for scope in scopes if scope["id"] != parts[-1]]


@pytest.fixture(autouse=True)
def public_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_public_url", AnyHttpUrl("https://aizk.test"))


def policy(fake: FakeLogto) -> LogtoPolicy:
    return LogtoPolicy(cast("LogtoClient", fake))


def test_policy_audit_is_clean_without_changing_unrelated_roles() -> None:
    fake = FakeLogto(LogtoState.clean())

    report = dbutil.run(policy(fake).audit())

    assert report.clean
    assert report.changes == ()
    assert fake.calls == []
    assert fake.invalidations == 0


def test_policy_apply_repairs_every_managed_layer_and_is_idempotent() -> None:
    state = LogtoState.clean()
    state.resources[0].update({"name": "Old", "accessTokenTtl": 60})
    state.resource_scopes = []
    state.roles = [
        {
            "id": "wrong-user",
            "name": settings.logto_user_role,
            "type": "MachineToMachine",
        },
        {"id": "old-editor", "name": "aizk-editor", "type": "User"},
        {"id": "external", "name": "external", "type": "User"},
    ]
    state.role_scopes = {}
    state.organization_scopes = []
    state.organization_roles = [
        {
            "id": "org-role-admin",
            "name": "admin",
            "description": "Old",
            "type": "MachineToMachine",
            "scopes": [],
        },
        {
            "id": "org-role-viewer",
            "name": "viewer",
            "description": settings.logto_organization_roles["viewer"],
            "type": "User",
            "scopes": [
                {
                    "id": "scope-write-memory",
                    "name": settings.logto_write_permission,
                }
            ],
        },
        {
            "id": "org-role-custom",
            "name": "custom",
            "description": "Untouched",
            "type": "User",
            "scopes": [{"id": "other", "name": "other"}],
        },
    ]
    fake = FakeLogto(state)

    report = dbutil.run(policy(fake).apply())
    mutating_passes = fake.invalidations
    second = dbutil.run(policy(fake).apply())

    assert report.clean and len(report.changes) == 12
    assert second == type(second)(clean=True)
    # Every mutating pass evicts the cached authority snapshots; the clean apply keeps them.
    assert mutating_passes > 0
    assert fake.invalidations == mutating_passes
    assert {role["name"] for role in state.roles} == {settings.logto_user_role, "external"}
    assert next(role for role in state.roles if role["name"] == "external")["id"] == "external"
    custom = next(role for role in state.organization_roles if role["name"] == "custom")
    assert custom["scopes"] == [{"id": "other", "name": "other"}]
    viewer = next(role for role in state.organization_roles if role["name"] == "viewer")
    assert viewer["scopes"] == []
    assert {role["name"] for role in state.organization_roles} == {
        "admin",
        "editor",
        "viewer",
        "custom",
    }


def test_policy_reports_existing_user_role_and_permission_drift() -> None:
    state = LogtoState.clean()
    state.roles[0].update({"description": "Old", "isDefault": False})
    state.role_scopes["role-user"] = []
    admin = next(role for role in state.organization_roles if role["name"] == "admin")
    cast("list[JsonObject]", admin["scopes"]).clear()

    report = dbutil.run(policy(FakeLogto(state)).audit())

    assert not report.clean
    assert report.changes == (
        "update default user role aizk-user",
        "grant API permissions to aizk-user",
        "grant organization permissions to admin",
    )


def test_policy_removes_the_retired_invitation_permission() -> None:
    state = LogtoState.clean()
    invitation: JsonObject = {
        "id": "scope-invite-member",
        "name": "invite:member",
        "description": "Invite a person",
    }
    state.organization_scopes.append(invitation)
    admin = next(role for role in state.organization_roles if role["name"] == "admin")
    cast("list[JsonObject]", admin["scopes"]).append(invitation)
    fake = FakeLogto(state)

    report = dbutil.run(policy(fake).apply())

    assert report.clean
    assert report.changes == (
        "revoke invite:member from admin",
        "delete retired organization permission invite:member",
    )
    assert invitation not in state.organization_scopes
    assert invitation not in cast("list[JsonObject]", admin["scopes"])


def test_policy_stops_when_the_management_api_never_reflects_writes() -> None:
    fake = FakeLogto(LogtoState(), mutable=False)

    with pytest.raises(RuntimeError, match="did not converge"):
        dbutil.run(policy(fake).apply())

    assert len(fake.calls) == 8
    assert fake.invalidations == 8
