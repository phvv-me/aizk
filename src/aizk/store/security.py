from collections.abc import Iterable
from re import compile
from typing import cast

import rls
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlglot import exp
from sqlmodel import select

_DOLLAR_QUOTE = compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_PG_POLICIES = sa.table(
    "pg_policies",
    sa.column("schemaname", sa.Text()),
    sa.column("tablename", sa.Text()),
    sa.column("policyname", sa.Text()),
    sa.column("permissive", sa.Text()),
    schema="pg_catalog",
)

type PolicyPermission = tuple[str, str, str, str]


class _CockroachPolicy(rls.CompiledPolicy):
    """Normalize CockroachDB deparser casts before PostgreSQL policy comparison."""

    @staticmethod
    def postgres_casts(clause: str) -> str:
        """Translate triple-colon casts without changing quoted policy values."""
        normalized: list[str] = []
        quote: str | None = None
        index = 0
        while index < len(clause):
            if quote is not None:
                if clause.startswith(quote, index):
                    normalized.append(quote)
                    index += len(quote)
                    if quote in {"'", '"'} and clause.startswith(quote, index):
                        normalized.append(quote)
                        index += len(quote)
                    else:
                        quote = None
                else:
                    normalized.append(clause[index])
                    index += 1
                continue
            if clause.startswith(":::", index):
                normalized.append("::")
                index += 3
                continue
            if clause[index] in {"'", '"'}:
                quote = clause[index]
            elif dollar_quote := _DOLLAR_QUOTE.match(clause, index):
                quote = dollar_quote.group()
                normalized.append(quote)
                index += len(quote)
                continue
            normalized.append(clause[index])
            index += 1
        return "".join(normalized)

    @classmethod
    def _normalize(cls, clause: str | None, table: str) -> str | None:
        prepared = cls.postgres_casts(clause) if clause is not None else None
        return super()._normalize(prepared, table)

    @staticmethod
    def _rewrite(node: exp.Expr, table: str) -> exp.Expr:
        if isinstance(node, (exp.Cast, exp.TryCast)):
            target = node.args.get("to")
            if (
                isinstance(node.this, exp.Literal)
                and node.this.is_number
                and isinstance(target, exp.DataType)
                and target.this
                in {
                    exp.DataType.Type.SMALLINT,
                    exp.DataType.Type.INT,
                    exp.DataType.Type.BIGINT,
                }
            ):
                return node.this
        if (
            isinstance(node, exp.Dot)
            and isinstance(node.this, exp.Identifier)
            and node.this.name.casefold() == "public"
            and isinstance(node.expression, exp.Anonymous)
        ):
            return node.expression
        return rls.CompiledPolicy._rewrite(node, table)


class RLSVerifier:
    """Verify one RLS catalog with the active database's reflected SQL dialect."""

    def __init__(self, catalog: rls.Catalog) -> None:
        self.catalog = catalog

    @staticmethod
    def cockroach_state(
        state: rls.RLSState, permissions: dict[str, bool] | None = None
    ) -> rls.RLSState:
        """Use Cockroach-aware policies while preserving flags and policy data."""
        return state.model_copy(
            update={
                "policies": tuple(
                    _CockroachPolicy.model_validate(
                        {
                            **policy.model_dump(),
                            "permissive": (permissions or {}).get(policy.name, policy.permissive),
                        }
                    )
                    for policy in state.policies
                )
            }
        )

    @staticmethod
    def cockroach_permissions(
        connection: Connection, tables: Iterable[sa.Table]
    ) -> dict[tuple[str, str, str], bool]:
        """Read policy modes without the released reflector's uppercase assumption."""
        keys = sorted(
            (table.schema or connection.dialect.default_schema_name or "public", table.name)
            for table in tables
        )
        statement = select(
            _PG_POLICIES.c.schemaname,
            _PG_POLICIES.c.tablename,
            _PG_POLICIES.c.policyname,
            _PG_POLICIES.c.permissive,
        ).where(sa.tuple_(_PG_POLICIES.c.schemaname, _PG_POLICIES.c.tablename).in_(keys))
        rows = cast(Iterable[PolicyPermission], connection.execute(statement))
        return {
            (schema, table, policy): permission.casefold() == "permissive"
            for schema, table, policy, permission in rows
        }

    def verify(self, connection: Connection) -> list[str]:
        """Report every declared policy drift for this connection."""
        if connection.dialect.name != "cockroachdb":
            return self.catalog.verify(connection)
        live = self.catalog.inspect(connection)
        permissions = self.cockroach_permissions(connection, self.catalog.tables)
        violations: list[str] = []
        for table in self.catalog.tables:
            schema = table.schema or connection.dialect.default_schema_name or "public"
            table_permissions = {
                policy: permissive
                for (policy_schema, policy_table, policy), permissive in permissions.items()
                if policy_schema == schema and policy_table == table.name
            }
            state = self.cockroach_state(live[table], table_permissions)
            declared = self.catalog.state(table)
            if declared is None:
                if state.exists:
                    violations.append(f"{table.fullname} has undeclared row level security")
                continue
            violations.extend(self.cockroach_state(declared).diff(state, table.name))
        return violations
