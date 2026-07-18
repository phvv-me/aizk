from collections.abc import Iterable, Sequence
from functools import cache, cached_property
from types import TracebackType
from typing import TYPE_CHECKING, TypedDict, cast

import rls
from patos import FrozenModel
from pydantic import (
    UUID5,
    BaseModel,
    Field,
    PrivateAttr,
    TypeAdapter,
    model_serializer,
)
from sqlalchemy import Row
from sqlalchemy.sql.selectable import Select
from sqlmodel.sql.expression import SelectOfScalar

from ...config import settings
from ...config.settings import StatementValue
from ...exceptions import ScopeNotFoundError
from ...types import ScopeNames, Scopes
from .. import engine
from .organization import OrganizationStanding

if TYPE_CHECKING:
    from ..engine import Session


class ScopeTable(FrozenModel):
    """The complete PostgreSQL read, write, and singleton-public authority of one caller."""

    read: frozenset[UUID5] = frozenset()
    write: frozenset[UUID5] = frozenset()
    public: frozenset[UUID5] = frozenset()


class UserStatus(TypedDict):
    """Directory-safe Logto identity serialized by the MCP `status` tool."""

    name: str | None
    username: str | None
    avatar: str | None
    roles: tuple[str, ...]
    organizations: tuple[OrganizationStanding, ...]


class User(rls.Context, prefix="app"):
    """Carry one verified caller and its authority into transaction-local PostgreSQL RLS.

    `id` is stable provenance and the personal scope. `scopes` contains current
    authority resolved before the transaction. Entering the user opens an app-role
    transaction and writes this context through `SET LOCAL`, so pooled connections
    cannot leak one caller's standing into the next request.
    """

    _transactions: list[engine.SessionScope] = PrivateAttr(default_factory=list)

    id: UUID5 = Field(exclude=True)
    name: str | None = Field(default=None, exclude=True)
    username: str | None = Field(default=None, exclude=True)
    avatar: str | None = Field(default=None, exclude=True)
    roles: tuple[str, ...] = Field(default=(), exclude=True)
    scopes: ScopeTable = ScopeTable()
    organizations: tuple[OrganizationStanding, ...] = Field(default=(), exclude=True)

    @classmethod
    def authorized(
        cls,
        user_id: UUID5,
        read: Iterable[UUID5] = (),
        write: Iterable[UUID5] = (),
        public: Iterable[UUID5] = (),
        label: str | None = None,
        name: str | None = None,
        username: str | None = None,
        avatar: str | None = None,
        roles: Iterable[str] = (),
        organizations: Iterable[OrganizationStanding] = (),
    ) -> User:
        """Build a caller after an authentication boundary has verified every scope set."""
        writable = frozenset(write)
        return cls(
            id=user_id,
            name=name or label,
            username=username,
            avatar=avatar,
            roles=tuple(roles),
            scopes=ScopeTable(
                read=frozenset(read),
                write=writable,
                public=frozenset(public),
            ),
            organizations=tuple(
                organization.model_copy(update={"writable": organization.id in writable})
                for organization in organizations
            ),
        )

    @classmethod
    def private(cls, user_id: UUID5) -> User:
        """Build a local caller that can read and write only its personal scope."""
        personal = () if user_id == settings.anonymous_user_id else (user_id,)
        return cls.authorized(user_id, read=personal, write=personal)

    @classmethod
    def system(cls, scopes: Iterable[UUID5] = ()) -> User:
        """Build background authority for one exact maintenance scope set."""
        key = frozenset(scopes) or frozenset({settings.system_user_id})
        return cls.authorized(settings.system_user_id, read=key, write=key)

    async def __aenter__(self) -> Session:
        """Open one short app-role transaction acting as this caller."""
        transaction = self.app
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

    def session(self) -> engine.SessionScope:
        """Open one caller-bound app session for workflows that need several transactions."""
        return engine.Database.app().session(self)

    @property
    def app(self) -> engine.SessionScope:
        """Open one app-role transaction whose rows PostgreSQL filters for this caller."""
        return engine.Database.app().transaction(self)

    @property
    def owner(self) -> engine.SessionScope:
        """Open an RLS-bypassing maintenance transaction only for the system identity."""
        if self.id != settings.system_user_id:
            raise PermissionError("only the system caller may use the database owner role")
        return engine.Database.owner().transaction(self)

    @property
    def exec(self) -> Exec:
        """Typed one-statement execution as this caller, indexed by the row model."""
        return Exec(user=self)

    def is_anonymous(self) -> bool:
        """Return whether this is the read-only fallback identity used by public mode."""
        return self.id == settings.anonymous_user_id

    @cached_property
    def label(self) -> str | None:
        """Return the best human-readable caller identity supplied by Logto."""
        return self.name or self.username

    @cached_property
    def public_organizations(self) -> tuple[OrganizationStanding, ...]:
        """Return organizations Logto marks public through their custom data."""
        return tuple(item for item in self.organizations if item.public)

    @cached_property
    def writable_organizations(self) -> tuple[OrganizationStanding, ...]:
        """Return organizations whose Logto permissions satisfy AIZK's write policy."""
        return tuple(item for item in self.organizations if item.writable)

    @cached_property
    def organization_ids(self) -> dict[str, UUID5]:
        """Index the resolved Logto organization names by their stable AIZK scope IDs."""
        return {item.name: item.id for item in self.organizations}

    def scope_labels(self, scopes: Iterable[UUID5]) -> tuple[str, ...]:
        """Label scope ids in order as Private, the organization name, or Shared."""
        names = {item.id: item.name for item in self.organizations}
        return tuple(
            "Private" if scope == self.id else names.get(scope, "Shared") for scope in scopes
        )

    @model_serializer
    def serialize_status(self) -> UserStatus:
        """Serialize the useful Logto directory while excluding internal and personal fields."""
        return UserStatus(
            name=self.name,
            username=self.username,
            avatar=self.avatar,
            roles=self.roles,
            organizations=tuple(sorted(self.organizations, key=lambda item: item.name)),
        )

    def write_scope(self, names: ScopeNames | None = None) -> Scopes:
        """Resolve organization names into one destination covered by write standing."""
        try:
            target = (
                frozenset(self.organization_ids[name] for name in names)
                if names
                else frozenset({self.id})
            )
        except KeyError as missing:
            raise ScopeNotFoundError(f"no writable scope named {missing.args[0]!r}") from missing
        if not target <= self.scopes.write:
            raise ScopeNotFoundError("Logto does not grant write permission in every target scope")
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

    async def __call__(
        self,
        statement: Select,
        /,
        **binds: StatementValue,
    ) -> tuple[RowT, ...]:
        """Execute with the statement's settings-named binds merged under the explicit ones.

        statement: the compiled selectable to run.
        binds: named bind parameters, overriding any settings value of the same name.
        """
        parameters = {**settings.for_statement(statement), **binds}
        async with self.user as session:
            rows = (await session.exec(statement, params=parameters)).all()
        if isinstance(statement, SelectOfScalar):
            field = next(iter(statement.selected_columns)).key
            if len(self.model.model_fields) == 1 and field in self.model.model_fields:
                return tuple(self.model.model_validate({field: value}) for value in rows)
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
