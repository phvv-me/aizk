import dbutil
import pytest
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from patos import FrozenModel
from pydantic import ValidationError
from sqlalchemy import Integer, bindparam, literal
from sqlmodel import select

from aizk.config import settings
from aizk.exceptions import ScopeNotFoundError
from aizk.store.engine import Database
from aizk.store.identity import OrganizationMember, OrganizationStanding, User
from aizk.store.identity.user import RowStatement


class ProbeRow(FrozenModel):
    """One integer column as a typed exec row."""

    x: int


@given(subject=st.text(min_size=1), scope=st.text(min_size=1))
def test_external_ids_are_deterministic_and_domain_separated(subject: str, scope: str) -> None:
    assert settings.subject_id(subject) == settings.subject_id(subject)
    assert settings.scope_id(scope) == settings.scope_id(scope)
    assert settings.subject_id(subject) != settings.scope_id(subject)


@pytest.mark.parametrize(
    ("binds", "expected"),
    [({}, settings.fusion_depth), ({"fusion_depth": 41}, 41)],
    ids=["settings", "explicit"],
)
def test_exec_merges_settings_under_explicit_binds(
    migrated_db: None, binds: dict[str, int], expected: int
) -> None:
    statement = select(bindparam("fusion_depth", type_=Integer).label("x"))
    rows = dbutil.run(User.system().exec[ProbeRow](statement, **binds))
    assert rows == (ProbeRow(x=expected),)


def test_exec_rejects_rows_that_do_not_fit_the_model(migrated_db: None) -> None:
    statement = select(literal("not a number").label("x"))
    with pytest.raises(ValidationError):
        dbutil.run(User.system().exec[ProbeRow](statement))


def test_row_validator_is_cached_per_model() -> None:
    assert RowStatement.row_validator(ProbeRow) is RowStatement.row_validator(ProbeRow)


def test_session_scope_rejects_invalid_lifecycle(migrated_db: None) -> None:
    scope = Database.app().session(User.system())

    with pytest.raises(RuntimeError, match="not open"):
        dbutil.run(scope.__aexit__(None, None, None))

    async def enter_twice() -> None:
        scope = User.system().app
        await scope.__aenter__()
        try:
            with pytest.raises(RuntimeError, match="already open"):
                await scope.__aenter__()
        finally:
            await scope.__aexit__(None, None, None)

    dbutil.run(enter_twice())


def test_user_role_transactions_bind_the_same_caller(migrated_db: None) -> None:
    async def assert_roles() -> None:
        user = User.system()
        async with user.app as app:
            assert app.user is user
        async with user.owner as owner:
            assert owner.user is user

    dbutil.run(assert_roles())
    with pytest.raises(PermissionError, match="only the system caller"):
        _ = User.private(uuid5()).owner


def test_write_scope_defaults_to_personal_and_resolves_named_intersections() -> None:
    user_id = uuid5()
    first, second = uuid5(), uuid5()
    user = User.authorized(
        user_id,
        write=(user_id, first, second),
        organizations=(
            OrganizationStanding(id=first, name="first"),
            OrganizationStanding(id=second, name="second"),
        ),
    )

    assert user.write_scope() == frozenset({user_id})
    assert user.write_scope(["first"]) == frozenset({first})
    assert user.write_scope(["first", "second"]) == frozenset({first, second})
    with pytest.raises(ScopeNotFoundError, match="no writable scope"):
        user.write_scope(["missing"])

    reader = User.authorized(
        user_id,
        read=(user_id, first),
        organizations=(OrganizationStanding(id=first, name="first"),),
    )
    with pytest.raises(ScopeNotFoundError, match="Logto does not grant write"):
        reader.write_scope(["first"])


def test_user_indexes_current_organization_directory_and_standing() -> None:
    user_id, public_id, private_id = uuid5(), uuid5(), uuid5()
    named = OrganizationMember(name="Pedro", username="pedro")
    username_only = OrganizationMember(username="colleague")
    unnamed = OrganizationMember()
    public = OrganizationStanding(
        id=public_id,
        name="Docs",
        public=True,
        members=(named, username_only, unnamed),
    )
    private = OrganizationStanding(id=private_id, name="Private Lab")
    user = User.authorized(
        user_id,
        read=(user_id, public_id, private_id),
        write=(user_id, private_id),
        organizations=(public, private),
    )

    assert named.label == "Pedro"
    assert username_only.label == "colleague"
    assert unnamed.label == "unnamed member"
    assert public.members_by_name == {
        "Pedro": named,
        "colleague": username_only,
        "unnamed member": unnamed,
    }
    assert user.public_organizations == (public,)
    assert user.writable_organizations == (user.organizations[1],)
