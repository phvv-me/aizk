import uuid

import dbutil
import pytest
from hypothesis import given
from hypothesis import strategies as st
from patos import FrozenModel
from pydantic import ValidationError
from sqlalchemy import Integer, bindparam, literal, select

from aizk.config import settings
from aizk.exceptions import ScopeNotFoundError
from aizk.store.identity import User
from aizk.store.identity.user import RowStatement


class ProbeRow(FrozenModel):
    """One integer column as a typed exec row."""

    x: int


@given(subject=st.text(min_size=1), scope=st.text(min_size=1))
def test_external_ids_are_deterministic_and_domain_separated(subject: str, scope: str) -> None:
    assert settings.subject_id(subject) == settings.subject_id(subject)
    assert settings.scope_id(scope) == settings.scope_id(scope)
    assert settings.subject_id(subject) != settings.scope_id(subject)


def test_exec_runs_one_statement_and_merges_settings_binds(migrated_db: None) -> None:
    statement = select(bindparam("fusion_depth", type_=Integer).label("x"))
    rows = dbutil.run(User.system().exec[ProbeRow](statement))
    assert rows == (ProbeRow(x=settings.fusion_depth),)


def test_exec_explicit_binds_win_over_settings(migrated_db: None) -> None:
    statement = select(bindparam("fusion_depth", type_=Integer).label("x"))
    rows = dbutil.run(User.system().exec[ProbeRow](statement, fusion_depth=41))
    assert rows == (ProbeRow(x=41),)


def test_exec_rejects_rows_that_do_not_fit_the_model(migrated_db: None) -> None:
    statement = select(literal("not a number").label("x"))
    with pytest.raises(ValidationError):
        dbutil.run(User.system().exec[ProbeRow](statement))


def test_row_validator_is_cached_per_model() -> None:
    assert RowStatement.row_validator(ProbeRow) is RowStatement.row_validator(ProbeRow)


def test_write_scope_defaults_to_personal_and_resolves_named_intersections() -> None:
    user_id = uuid.uuid7()
    first, second = uuid.uuid7(), uuid.uuid7()
    user = User.authorized(
        user_id,
        write=(user_id, first, second),
        names={"first": first, "second": second},
    )

    assert user.write_scope() == frozenset({user_id})
    assert user.write_scope(["first"]) == frozenset({first})
    assert user.write_scope(["first", "second"]) == frozenset({first, second})
    with pytest.raises(ScopeNotFoundError, match="no writable scope"):
        user.write_scope(["missing"])

    reader = User.authorized(user_id, read=(user_id, first), names={"first": first})
    with pytest.raises(ScopeNotFoundError, match="editor or admin"):
        reader.write_scope(["first"])
