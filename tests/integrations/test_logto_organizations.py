from unittest.mock import AsyncMock, Mock

import dbutil
import httpx
import pytest

from aizk.config import settings
from aizk.integrations.logto import (
    Account,
    LogtoClient,
    Member,
    Org,
    OrganizationManager,
    Role,
)
from aizk.store.identity import User


def current_user(subject: str = "user-1") -> User:
    """Build one authenticated identity matching a raw Logto subject."""
    user_id = settings.subject_id(subject)
    return User.authorized(user_id, read=(user_id,), write=(user_id,))


def response(method: str, path: str, status: int = 201, **payload: str) -> httpx.Response:
    """Build one requested Management API response for an async mock."""
    return httpx.Response(
        status,
        json=payload or None,
        request=httpx.Request(method, f"https://auth.test/{path}"),
    )


def failure(method: str, path: str) -> httpx.HTTPStatusError:
    """Build one concrete Management API failure."""
    failed = response(method, path, status=503)
    return httpx.HTTPStatusError("unavailable", request=failed.request, response=failed)


def manager(monkeypatch: pytest.MonkeyPatch) -> tuple[OrganizationManager, LogtoClient]:
    """Build one isolated organization manager with no network calls."""
    client = LogtoClient()
    dbutil.run(client.close())
    monkeypatch.setattr(client.caches, "invalidate", Mock())
    return OrganizationManager(client=client, user=current_user(), subject="user-1"), client


def test_manager_rejects_anonymous_or_mismatched_identity() -> None:
    client = LogtoClient()
    with pytest.raises(PermissionError):
        OrganizationManager(
            client=client, user=User.private(settings.anonymous_user_id), subject="user-1"
        )
    with pytest.raises(PermissionError):
        OrganizationManager(client=client, user=current_user("other"), subject="user-1")
    dbutil.run(client.close())


def test_create_makes_one_private_organization_and_creator_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    invalidate = Mock()
    monkeypatch.setattr(client.caches, "invalidate", invalidate)
    monkeypatch.setattr(client, "organizations", AsyncMock(return_value=()))
    monkeypatch.setattr(
        client,
        "organization_roles",
        AsyncMock(return_value=(Role(id="admin-role", name="admin"),)),
    )
    management = AsyncMock(
        side_effect=(
            response("POST", "api/organizations", id="org-1", name="Research"),
            response("POST", "api/organizations/org-1/users"),
            response("POST", "api/organizations/org-1/users/user-1/roles"),
        )
    )
    monkeypatch.setattr(client, "management", management)

    result = dbutil.run(service.create(" Research ", " Shared work "))

    assert result.organization == "Research"
    assert management.await_args_list[0].kwargs["payload"] == {
        "name": "Research",
        "description": "Shared work",
        "customData": {"public": False},
    }
    assert management.await_args_list[1].kwargs["payload"] == {"userIds": ["user-1"]}
    assert management.await_args_list[2].kwargs["payload"] == {
        "organizationRoleIds": ["admin-role"]
    }
    invalidate.assert_called_once_with("user-1", organization_ids=("org-1",))


@pytest.mark.parametrize("name", ["", "  "])
def test_create_rejects_empty_or_existing_names(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    service, client = manager(monkeypatch)
    with pytest.raises(ValueError, match="required"):
        dbutil.run(service.create(name))

    monkeypatch.setattr(
        client,
        "organizations",
        AsyncMock(return_value=(Org(id="existing", name="Research"),)),
    )
    with pytest.raises(ValueError, match="already in use"):
        dbutil.run(service.create("research"))


def test_create_removes_an_incomplete_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    monkeypatch.setattr(client, "organizations", AsyncMock(return_value=()))
    monkeypatch.setattr(
        client,
        "organization_roles",
        AsyncMock(return_value=(Role(id="admin-role", name="admin"),)),
    )
    management = AsyncMock(
        side_effect=(
            response("POST", "api/organizations", id="org-1", name="Research"),
            failure("POST", "api/organizations/org-1/users"),
            response("DELETE", "api/organizations/org-1", status=204),
        )
    )
    monkeypatch.setattr(client, "management", management)

    with pytest.raises(httpx.HTTPStatusError):
        dbutil.run(service.create("Research"))

    assert management.await_args_list[-1].args == ("DELETE", "api/organizations/org-1")


def test_add_uses_exact_email_and_cleans_up_a_role_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    organization = Org(
        id="org-1",
        name="Research",
        scopes=[{"id": "manage", "name": "manage:member"}],
    )
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=(organization,)))
    account_by_email = AsyncMock(return_value=Account(id="user-2", primaryEmail="lab@example.com"))
    monkeypatch.setattr(client, "account_by_email", account_by_email)
    monkeypatch.setattr(
        client,
        "organization_roles",
        AsyncMock(return_value=(Role(id="viewer-role", name="viewer"),)),
    )
    management = AsyncMock(
        side_effect=(
            response("POST", "api/organizations/org-1/users"),
            failure("POST", "api/organizations/org-1/users/user-2/roles"),
            response("DELETE", "api/organizations/org-1/users/user-2", status=204),
        )
    )
    monkeypatch.setattr(client, "management", management)

    with pytest.raises(httpx.HTTPStatusError):
        dbutil.run(service.add("Research", " LAB@example.com ", "viewer"))

    account_by_email.assert_awaited_once_with("lab@example.com")
    assert management.await_args_list[-1].args == (
        "DELETE",
        "api/organizations/org-1/users/user-2",
    )


def test_add_assigns_one_role_and_invalidates_current_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    organization = Org(
        id="org-1",
        name="Research",
        scopes=[{"id": "manage", "name": "manage:member"}],
    )
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=(organization,)))
    monkeypatch.setattr(
        client,
        "account_by_email",
        AsyncMock(return_value=Account(id="user-2", primaryEmail="lab@example.com")),
    )
    monkeypatch.setattr(
        client,
        "organization_roles",
        AsyncMock(return_value=(Role(id="viewer-role", name="viewer"),)),
    )
    management = AsyncMock(
        side_effect=(
            response("POST", "api/organizations/org-1/users"),
            response("POST", "api/organizations/org-1/users/user-2/roles"),
        )
    )
    invalidated = Mock()
    monkeypatch.setattr(client, "management", management)
    monkeypatch.setattr(client.caches, "invalidate", invalidated)

    change = dbutil.run(service.add("Research", "lab@example.com", "viewer"))

    assert change.organization == "Research"
    assert change.member == "lab@example.com"
    assert management.await_count == 2
    invalidated.assert_called_once_with(
        "user-1",
        "user-2",
        organization_ids=("org-1",),
    )


@pytest.mark.parametrize(
    ("email", "account", "message"),
    [
        ("not-an-email", None, "exact email"),
        ("missing@example.com", None, "create an AIZK account"),
    ],
)
def test_add_rejects_invalid_or_unknown_accounts(
    monkeypatch: pytest.MonkeyPatch,
    email: str,
    account: Account | None,
    message: str,
) -> None:
    service, client = manager(monkeypatch)
    organization = Org(
        id="org-1",
        name="Research",
        scopes=[{"id": "manage", "name": "manage:member"}],
    )
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=(organization,)))
    monkeypatch.setattr(client, "account_by_email", AsyncMock(return_value=account))

    with pytest.raises(ValueError, match=message):
        dbutil.run(service.add("Research", email, "viewer"))


def test_add_rejects_existing_member_and_unknown_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    account = Account(id="user-2", primaryEmail="lab@example.com")
    member = Member(id="user-2", username="lab")
    organization = Org(
        id="org-1",
        name="Research",
        members=(member,),
        scopes=[{"id": "manage", "name": "manage:member"}],
    )
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=(organization,)))
    monkeypatch.setattr(client, "account_by_email", AsyncMock(return_value=account))

    with pytest.raises(ValueError, match="already a member"):
        dbutil.run(service.add("Research", "lab@example.com", "viewer"))
    with pytest.raises(ValueError, match="supported"):
        dbutil.run(service.role("owner"))


def test_set_role_and_remove_recheck_live_scope_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    member = Member(id="user-2", name="Lab Mate")
    bystander = Member(id="user-3", name="Bystander")
    organization = Org(
        id="org-1",
        name="Research",
        members=(member, bystander),
        scopes=[
            {"id": "manage", "name": "manage:member"},
            {"id": "delete", "name": "delete:member"},
        ],
    )
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=(organization,)))
    monkeypatch.setattr(
        client,
        "organization_roles",
        AsyncMock(return_value=(Role(id="editor-role", name="editor"),)),
    )
    management = AsyncMock(
        side_effect=(
            response("PUT", "api/organizations/org-1/users/user-2/roles"),
            response("DELETE", "api/organizations/org-1/users/user-2", status=204),
        )
    )
    invalidated = Mock()
    monkeypatch.setattr(client, "management", management)
    monkeypatch.setattr(client.caches, "invalidate", invalidated)

    changed = dbutil.run(service.set_role("Research", "user-2", "editor"))
    removed = dbutil.run(service.remove("Research", "user-2"))

    assert changed.member == removed.member == "Lab Mate"
    assert management.await_args_list[0].args[0] == "PUT"
    assert management.await_args_list[1].args[0] == "DELETE"
    assert invalidated.call_args_list == [
        (
            ("user-1", "user-2", "user-3"),
            {"organization_ids": ("org-1",)},
        ),
        (
            ("user-1", "user-2", "user-3"),
            {"organization_ids": ("org-1",)},
        ),
    ]


def test_final_administrator_cannot_be_demoted_or_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    admin = Role(id="admin-role", name="admin")
    final_admin = Member(id="user-2", name="Final Admin", roles=(admin,))
    organization = Org(
        id="org-1",
        name="Research",
        members=(Member(id="user-1", name="Manager"), final_admin),
        scopes=(
            {"id": "manage", "name": "manage:member"},
            {"id": "delete", "name": "delete:member"},
        ),
    )
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=(organization,)))
    monkeypatch.setattr(
        client,
        "organization_roles",
        AsyncMock(return_value=(Role(id="editor-role", name="editor"),)),
    )
    management = AsyncMock()
    monkeypatch.setattr(client, "management", management)

    with pytest.raises(ValueError, match="final admin"):
        dbutil.run(service.set_role("Research", "user-2", "editor"))
    with pytest.raises(ValueError, match="final admin"):
        dbutil.run(service.remove("Research", "user-2"))

    management.assert_not_awaited()


def test_administrator_can_change_when_another_admin_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    admin = Role(id="admin-role", name="admin")
    target = Member(id="user-2", name="Other Admin", roles=(admin,))
    organization = Org(
        id="org-1",
        name="Research",
        members=(Member(id="user-1", name="Admin", roles=(admin,)), target),
        scopes=({"id": "manage", "name": "manage:member"},),
    )
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=(organization,)))
    monkeypatch.setattr(
        client,
        "organization_roles",
        AsyncMock(return_value=(Role(id="editor-role", name="editor"),)),
    )
    management = AsyncMock(
        return_value=response("PUT", "api/organizations/org-1/users/user-2/roles")
    )
    monkeypatch.setattr(client, "management", management)

    changed = dbutil.run(service.set_role("Research", "user-2", "editor"))
    service.ensure_admin_survives(organization, target, admin)

    assert changed.member == "Other Admin"
    management.assert_awaited_once()


def test_manager_hides_unknown_organizations_and_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, client = manager(monkeypatch)
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=()))
    with pytest.raises(PermissionError, match="not permitted"):
        dbutil.run(service.organization("Hidden", "manage:member"))

    organization = Org(id="org-1", name="Research")
    with pytest.raises(ValueError, match="not a current"):
        service.member(organization, "missing")

    own = Org(
        id=organization.id,
        name=organization.name,
        members=(Member(id="user-1", username="me"),),
        scopes=({"id": "delete", "name": "delete:member"},),
    )
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=(own,)))
    with pytest.raises(ValueError, match="Transfer administration"):
        dbutil.run(service.remove("Research", "user-1"))


def test_role_requires_the_live_logto_template(monkeypatch: pytest.MonkeyPatch) -> None:
    service, client = manager(monkeypatch)
    monkeypatch.setattr(client, "organization_roles", AsyncMock(return_value=()))
    with pytest.raises(RuntimeError, match="no configured viewer"):
        dbutil.run(service.role("viewer"))
