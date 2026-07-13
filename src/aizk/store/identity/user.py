import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from functools import cache
from types import TracebackType
from typing import TYPE_CHECKING, cast

import rls
from patos import FrozenModel
from pydantic import BaseModel, Field, PrivateAttr, TypeAdapter
from sqlalchemy import Row
from sqlalchemy.sql.selectable import Select

from ...config import settings
from ...exceptions import ScopeNotFoundError
from ...types import ScopeNames, Scopes
from .. import engine

if TYPE_CHECKING:
    from ..engine import Session


class ScopeTable(FrozenModel):
    """Readable, writable, public, and selected scope sets for one caller."""

    read: frozenset[uuid.UUID] = frozenset()
    write: frozenset[uuid.UUID] = frozenset()
    public: frozenset[uuid.UUID] = frozenset()


class User(rls.Context, prefix="app"):
    """Caller identity, scope authority, and transaction-local RLS context."""

    _transactions: list[AbstractAsyncContextManager[Session]] = PrivateAttr(default_factory=list)

    id: uuid.UUID = Field(exclude=True)
    label: str | None = Field(default=None, exclude=True)
    scopes: ScopeTable = ScopeTable()
    names: dict[str, uuid.UUID] = Field(default_factory=dict, exclude=True)

    @classmethod
    def authorized(
        cls,
        user_id: uuid.UUID,
        read: Iterable[uuid.UUID] = (),
        write: Iterable[uuid.UUID] = (),
        public: Iterable[uuid.UUID] = (),
        label: str | None = None,
        names: Mapping[str, uuid.UUID] | None = None,
    ) -> User:
        """Build a caller from already verified scope authority."""
        return cls(
            id=user_id,
            label=label,
            scopes=ScopeTable(
                read=frozenset(read),
                write=frozenset(write),
                public=frozenset(public),
            ),
            names=dict(names or {}),
        )

    @classmethod
    def private(cls, user_id: uuid.UUID) -> User:
        """Build a caller with authority over its personal scope."""
        personal = () if user_id == settings.anonymous_user_id else (user_id,)
        return cls.authorized(user_id, read=personal, write=personal)

    @classmethod
    def system(cls, scopes: Iterable[uuid.UUID] = ()) -> User:
        """Build the system identity over one exact scope set."""
        key = frozenset(scopes) or frozenset({settings.system_user_id})
        return cls.authorized(settings.system_user_id, read=key, write=key)

    async def __aenter__(self) -> Session:
        """Open one short app-role transaction acting as this caller."""
        transaction = engine.transaction(self)
        opened = await transaction.__aenter__()
        self._transactions.append(transaction)
        return opened

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Commit or roll back the innermost transaction this caller opened."""
        return await self._transactions.pop().__aexit__(exc_type, exc, traceback)

    @property
    def exec(self) -> Exec:
        """Typed one-statement execution as this caller, indexed by the row model."""
        return Exec(user=self)

    def is_anonymous(self) -> bool:
        """Whether this is the unauthenticated public reader."""
        return self.id == settings.anonymous_user_id

    def write_scope(self, names: ScopeNames | None = None) -> Scopes:
        """Resolve and authorize one explicit write destination."""
        try:
            target = (
                frozenset(self.names[name] for name in names) if names else frozenset({self.id})
            )
        except KeyError as missing:
            raise ScopeNotFoundError(f"no writable scope named {missing.args[0]!r}") from missing
        if not target <= self.scopes.write:
            raise ScopeNotFoundError("write needs editor or admin standing in every target scope")
        return target


class Exec(FrozenModel):
    """Index by a row model to run one statement as one caller transaction.

    `await user.exec[Candidate](statement, qvec=vector, k=8)` opens a short transaction
    as `user`, executes the statement with the keyword binds layered over the settings
    values it names, and validates every returned row into a `Candidate` tuple.
    """

    user: User

    def __getitem__[RowT: BaseModel](self, model: type[RowT]) -> RowStatement[RowT]:
        """Bind one row model, yielding the awaitable statement runner."""
        return RowStatement(user=self.user, model=model)


class RowStatement[RowT: BaseModel](FrozenModel):
    """One statement run as one caller transaction and validated into typed rows."""

    user: User
    model: type[RowT]

    async def __call__(self, statement: Select, /, **binds: object) -> tuple[RowT, ...]:
        """Execute with the statement's settings-named binds merged under the explicit ones.

        statement: the compiled selectable to run.
        binds: named bind parameters, overriding any settings value of the same name.
        """
        parameters = {**settings.for_statement(statement), **binds}
        async with self.user as session:
            rows = (await session.exec(statement, params=parameters)).all()
        return self.validate_rows(rows)

    def validate_rows(self, rows: Sequence[Row]) -> tuple[RowT, ...]:
        """Validate statement rows into this runner's row model by attribute access."""
        validator = cast("TypeAdapter[RowT]", self.row_validator(self.model))
        return tuple(validator.validate_python(row, from_attributes=True) for row in rows)

    @staticmethod
    @cache
    def row_validator(model: type[BaseModel]) -> TypeAdapter[BaseModel]:
        """One reused validator per row model, shared by every statement returning it."""
        return TypeAdapter(model)
