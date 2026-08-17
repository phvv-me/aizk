from collections.abc import Callable, Sequence
from typing import cast
from uuid import UUID

import rls
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, CreatePolicy, DropPolicy
from sqlalchemy.dialects.postgresql import Policy as SQLPolicy
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select

_SCOPED_TABLES = {
    "artifact": (True, False, None),
    "artifact_content": (True, False, "artifact"),
    "chunk": (True, True, "document"),
    "community": (False, True, None),
    "document": (True, False, None),
    "entity_claim": (False, False, None),
    "fact_claim": (True, False, None),
    "profile": (True, False, None),
    "session_item": (True, False, None),
    "usage_event": (False, False, None),
    "watermark": (True, False, None),
}


class PolicyContextTransition:
    """Freeze the policy transition from legacy carriers to typed settings."""

    def __init__(self, app_role: str, *, cockroachdb: bool) -> None:
        self.app_role = app_role
        self.cockroachdb = cockroachdb

    def states(self, *, legacy: bool) -> dict[str, rls.RLSState]:
        """Build every policy state on one side of the transition."""
        states = {
            table: self.scoped_state(table, *shape, legacy=legacy)
            for table, shape in _SCOPED_TABLES.items()
        }
        states.update(
            {
                "blob": self.blob_state(legacy=legacy),
                "entity_content": self.content_state("entity_content", legacy=legacy),
                "fact_content": self.content_state("fact_content", legacy=legacy),
                "operator_snapshot": self.operator_state(legacy=legacy),
                "upload_capability": self.scoped_state(
                    "upload_capability",
                    False,
                    True,
                    None,
                    legacy=legacy,
                ),
            }
        )
        return states

    def replace(self, execute: Callable[[Executable], None], *, legacy: bool) -> None:
        """Replace policies while keeping row security enabled and forced."""
        before = self.states(legacy=not legacy)
        after = self.states(legacy=legacy)
        for table_name in sorted(after):
            table = sa.Table(table_name, sa.MetaData())
            names = tuple(
                dict.fromkeys(
                    policy.name
                    for state in (before[table_name], after[table_name])
                    for policy in state.policies
                )
            )
            for name in reversed(names):
                execute(DropPolicy(SQLPolicy(name, table), if_exists=True))
            for policy in after[table_name].policies:
                execute(
                    CreatePolicy(
                        SQLPolicy(
                            policy.name,
                            table,
                            command=policy.command.sql,
                            roles=policy.roles,
                            using=policy.using,
                            check=policy.check,
                            permissive=policy.permissive,
                        )
                    )
                )

    def scoped_state(
        self,
        table_name: str,
        mutable: bool,
        deletable: bool,
        read_through: str | None,
        *,
        legacy: bool,
    ) -> rls.RLSState:
        """Build one scoped table policy set for the selected carrier."""
        table = sa.table(
            table_name,
            sa.column("scopes", ARRAY(sa.Uuid())),
            *(sa.column(f"{read_through}_id", sa.Uuid()),) if read_through else (),
        )
        scopes = table.c.scopes
        readable = self.authority("read", legacy=legacy)
        writable = self.authority("write", legacy=legacy)
        public = self.authority("public", legacy=legacy)
        nonempty = sa.func.cardinality(scopes) > 0
        scoped_read = sa.and_(
            nonempty,
            sa.or_(
                scopes.op("<@")(readable),
                sa.and_(sa.func.cardinality(scopes) == 1, scopes.op("<@")(public)),
            ),
        )
        read = scoped_read
        parent_scope: ColumnElement[bool] = sa.true()
        if read_through:
            parent_id = table.c[f"{read_through}_id"]
            if self.cockroachdb:
                parent_scope = getattr(sa.func, f"aizk_{read_through}_visible")(parent_id, scopes)
            else:
                parent = sa.table(
                    read_through,
                    sa.column("id", sa.Uuid()),
                    sa.column("scopes", ARRAY(sa.Uuid())),
                )
                read = parent_id.in_(select(parent.c.id))
                parent_scope = sa.tuple_(parent_id, scopes).in_(
                    select(parent.c.id, parent.c.scopes)
                )
        write = sa.and_(nonempty, scopes.op("<@")(writable), parent_scope)
        policies = [
            rls.Policy.select(
                read,
                name=self.name("select", legacy=legacy),
                roles=(self.app_role,),
            ),
            rls.Policy.insert(
                write,
                name=self.name("insert", legacy=legacy),
                roles=(self.app_role,),
            ),
        ]
        if mutable:
            policies.append(
                rls.Policy.update(
                    write,
                    check=write,
                    name=self.name("update", legacy=legacy),
                    roles=(self.app_role,),
                )
            )
        if deletable:
            policies.append(
                rls.Policy.delete(
                    write,
                    name=self.name("delete", legacy=legacy),
                    roles=(self.app_role,),
                )
            )
        return rls.RLSState.declared(tuple(policies))

    def authority(self, permission: str, *, legacy: bool) -> ColumnElement[Sequence[UUID]]:
        """Build one authority setting from the old or current carrier."""
        if not legacy:
            return rls.current_setting(f"scopes.{permission}", ARRAY(sa.Uuid()), prefix="app")
        if self.cockroachdb:
            positions = {"read": 2, "write": 3, "public": 4}
            encoded = sa.func.nullif(
                sa.func.split_part(
                    sa.func.current_setting("application_name", True),
                    "|",
                    positions[permission],
                ),
                "",
            )
            return cast(ColumnElement[Sequence[UUID]], encoded.cast(ARRAY(sa.Uuid())))
        standing = rls.current_setting("scopes", JSONB(), prefix="app")
        values = (
            sa.func.jsonb_array_elements_text(standing.op("->")(permission))
            .table_valued("value")
            .render_derived()
        )
        return sa.func.array(select(sa.cast(values.c.value, sa.Uuid())).scalar_subquery())

    def operator_state(self, *, legacy: bool) -> rls.RLSState:
        """Build the operator snapshot policy for the selected carrier."""
        standing: ColumnElement[bool]
        if legacy and self.cockroachdb:
            standing = cast(
                ColumnElement[bool],
                sa.func.nullif(
                    sa.func.split_part(sa.func.current_setting("application_name", True), "|", 5),
                    "",
                ).cast(sa.Boolean()),
            )
        else:
            standing = rls.current_setting("operator", sa.Boolean(), prefix="app")
        return rls.RLSState.declared(
            (
                rls.Policy.select(
                    standing,
                    name=("operator_snapshot_read" if legacy else "rls_select"),
                    roles=(self.app_role,),
                ),
            )
        )

    def content_state(self, table_name: str, *, legacy: bool) -> rls.RLSState:
        """Build one immutable content policy set."""
        claim_name = "entity_claim" if table_name == "entity_content" else "fact_claim"
        content = sa.table(table_name, sa.column("id", sa.Uuid()))
        claim = sa.table(claim_name, sa.column("content_id", sa.Uuid()))
        readable = (
            getattr(sa.func, f"aizk_{table_name}_visible")(content.c.id)
            if self.cockroachdb
            else content.c.id.in_(select(claim.c.content_id))
        )
        return rls.RLSState.declared(
            (
                rls.Policy.select(
                    readable,
                    name=("content_read" if legacy else "rls_select"),
                    roles=(self.app_role,),
                ),
                rls.Policy.insert(
                    sa.true(),
                    name=("content_insert" if legacy else "rls_insert"),
                    roles=(self.app_role,),
                ),
            )
        )

    def blob_state(self, *, legacy: bool) -> rls.RLSState:
        """Build the blob visibility policy set."""
        blob = sa.table("blob", sa.column("id", sa.Uuid()))
        content = sa.table("artifact_content", sa.column("blob_id", sa.Uuid()))
        readable = (
            sa.func.aizk_blob_visible(blob.c.id)
            if self.cockroachdb
            else blob.c.id.in_(select(content.c.blob_id))
        )
        return rls.RLSState.declared(
            (
                rls.Policy.select(
                    readable,
                    name=("blob_read" if legacy else "rls_select"),
                    roles=(self.app_role,),
                ),
                rls.Policy.insert(
                    sa.true(),
                    name=("blob_insert" if legacy else "rls_insert"),
                    roles=(self.app_role,),
                ),
            )
        )

    @staticmethod
    def name(command: str, *, legacy: bool) -> str:
        """Resolve a scoped policy name on one side of the transition."""
        if not legacy:
            return f"rls_{command}"
        suffix = "read" if command == "select" else command
        return f"scope_{suffix}"
