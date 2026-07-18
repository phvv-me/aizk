from unittest.mock import AsyncMock

import dbutil
import pytest

from aizk.api.organizations import OrganizationDirectory, OrganizationView
from aizk.integrations.logto.models import Member, Org, Role


@pytest.mark.parametrize(
    ("permissions", "can_manage", "can_delete", "member_ids"),
    [
        ((), False, False, ()),
        (("manage:member",), True, False, ("user-2",)),
        (("delete:member",), False, True, ("user-2",)),
    ],
    ids=["reader", "manager", "remover"],
)
def test_organization_view_discloses_member_ids_only_with_exact_permission(
    permissions: tuple[str, ...],
    can_manage: bool,
    can_delete: bool,
    member_ids: tuple[str, ...],
) -> None:
    organization = Org(
        id="org-1",
        name="Research",
        description="Shared experiments",
        members=(
            Member(
                id="user-2",
                name="Lab Mate",
                roles=(Role(id="editor", name="editor"),),
            ),
        ),
        roles=(Role(id="admin", name="admin"),),
        scopes=tuple(
            {"id": f"permission-{index}", "name": name} for index, name in enumerate(permissions)
        ),
    )

    view = OrganizationView.from_org(organization)

    assert view.description == "Shared experiments"
    assert view.roles == ("admin",)
    assert view.can_manage_members is can_manage
    assert view.can_delete_members is can_delete
    assert tuple(member.id for member in view.members) == member_ids
    if view.members:
        assert view.members[0].label == "Lab Mate"
        assert view.members[0].roles == ("editor",)


def test_directory_loads_only_current_subject_memberships_in_name_order() -> None:
    client = AsyncMock()
    client.user_orgs = AsyncMock(
        return_value=(
            Org(id="z", name="Zeta"),
            Org(id="a", name="alpha", description=None),
        )
    )

    directory = dbutil.run(OrganizationDirectory.load(client, "user-1"))

    assert [item.name for item in directory.organizations] == ["alpha", "Zeta"]
    assert directory.organizations[0].description == ""
    assert client.user_orgs.await_args is not None
    assert client.user_orgs.await_args.args == ("user-1",)
    assert client.user_orgs.await_args.kwargs == {"fresh": True}
