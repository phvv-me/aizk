import re
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ColumnElement


class Command(StrEnum):
    """The SQL command one policy guards, never `ALL`.

    A `FOR ALL` policy's USING clause is also OR-ed into SELECT visibility, so a table's write
    predicate would leak past its own narrower read predicate; every policy here names exactly one
    command instead, and a table gets one `Policy` per command it needs to guard.
    """

    select = "SELECT"
    insert = "INSERT"
    update = "UPDATE"
    delete = "DELETE"


@dataclass(frozen=True, slots=True)
class Policy:
    """One `CREATE POLICY` a model declares, its clauses live SQLAlchemy boolean expressions.

    name: policy name, unique per table.
    command: the single command this policy guards.
    using: the USING clause, checked against every existing row a SELECT, UPDATE, or DELETE may
        touch. Absent for INSERT, which has no existing row to guard.
    check: the WITH CHECK clause, checked against the row an INSERT or UPDATE would leave behind.
        Absent for SELECT and DELETE, which write nothing.
    """

    name: str
    command: Command
    using: ColumnElement[bool] | None = None
    check: ColumnElement[bool] | None = None


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    """A `Policy` with its clauses already rendered to literal-inlined Postgres text.

    The shape an alembic migration file actually stores: a migration is plain text read back years
    later, so its ops carry compiled SQL strings rather than live `ColumnElement` objects tied to
    the model metadata current when the migration was generated.
    """

    name: str
    command: Command
    using: str | None = None
    check: str | None = None


def compile_expression(expression: ColumnElement[bool]) -> str:
    """Render a boolean SQLAlchemy expression to the literal-inlined Postgres text a policy stores.

    A `CREATE POLICY` clause takes no bind parameters, so every literal a comparison carries, a
    role name or a cast target, is inlined rather than left as a `%s` placeholder; `literal_binds`
    is the compiler flag that does the inlining.

    expression: the clause to compile, already built from `predicates.py`'s helpers.
    """
    return str(
        expression.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def compile_policy(policy: Policy) -> CompiledPolicy:
    """Compile a declared `Policy`'s live expressions into a migration-storable `CompiledPolicy`.

    policy: the declared policy, its clauses SQLAlchemy expressions over a model's own columns.
    """
    return CompiledPolicy(
        name=policy.name,
        command=policy.command,
        using=compile_expression(policy.using) if policy.using is not None else None,
        check=compile_expression(policy.check) if policy.check is not None else None,
    )


def create_statement(table: str, policy: CompiledPolicy) -> str:
    """The `CREATE POLICY` statement for one compiled policy on `table`.

    table: table the policy protects.
    policy: the compiled policy, already carrying literal-inlined clause text.
    """
    clause = ""
    if policy.using is not None:
        clause += f" USING ({policy.using})"
    if policy.check is not None:
        clause += f" WITH CHECK ({policy.check})"
    return f"CREATE POLICY {policy.name} ON {table} FOR {policy.command.value}{clause}"


def drop_statement(table: str, name: str) -> str:
    """The `DROP POLICY IF EXISTS` statement removing one named policy from `table`.

    table: table the policy currently protects.
    name: name of the policy to drop.
    """
    return f"DROP POLICY IF EXISTS {name} ON {table}"


# adapted from DelfinaCare's `rls._sql_gen.normalize_sql_policy_expression`
# (https://github.com/DelfinaCare/rls, MIT): fold both a `CAST(x AS uuid)` and Postgres's own
# `(x)::uuid` re-serialization of the same cast to one shape, then drop every space and
# parenthesis outright, since Postgres freely reparenthesizes and reflows a stored policy's
# `qual`/`with_check` (label spelling, extra grouping parens, cast style) in ways that leave the
# expression's tokens unchanged but its punctuation and layout unrecognizable next to a freshly
# compiled string. Comparing token content rather than structure trades away catching a
# precedence-only rewrite for never false-flagging an unchanged policy as drifted, the same
# trade the upstream algorithm makes. `= any (array[...])` folding is this port's own addition on
# top of the adapted algorithm: Postgres always deparses a `col IN (a, b)` comparison back as a
# ScalarArrayOpExpr, `col = ANY (ARRAY[a, b])`, never as the literal `IN` syntax a freshly compiled
# `.in_(...)` clause renders, so without this fold every `IN` predicate would read as permanently
# drifted against its own unchanged self.
_CAST_SUFFIX = re.compile(r"::\w+")
_CAST_AS = re.compile(r"\bas\s+\w+")


def normalize_expression(expression: str) -> str:
    """Fold a compiled or catalog-read clause to a form comparable across re-serializations.

    expression: `qual`/`with_check` text, either freshly compiled or read back from `pg_policies`.
    """
    folded = expression.lower()
    folded = _CAST_SUFFIX.sub("", folded)
    folded = _CAST_AS.sub("", folded)
    folded = re.sub(r"\s+", "", folded)
    folded = folded.replace("(", "").replace(")", "").replace("[", "").replace("]", "")
    folded = folded.replace("cast", "").replace("array", "")
    return folded.replace("=any", "in")
