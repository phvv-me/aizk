"""Canonicalize every ontology name to snake_case, deduping the case and spacing variants.

The catalog forked near-variants: PascalCase entity types beside snake_case predicates, a
case-sensitive `name` primary key, and no normalization at write time, so `RaptorSummary`,
`raptor summary`, and `Raptor Summary` could each mint their own row. `OntologyKind.canonical`
now folds every write through `inflection`, and this migration brings the existing rows to the
same canonical form. For each name whose canonical form differs, it mints the canonical row when
absent (a collision keeps the row already there), repoints the foreign-key children onto it, then
drops the old row, so the rename needs no `ON UPDATE CASCADE` and two variants that canonicalize
to one name merge onto a single row.
"""

from collections.abc import Sequence

import inflection
import sqlalchemy as sa

from alembic import op

revision: str = "0004_ontology_snake_case"
down_revision: str | None = "0003_rls_writeguard_recall_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def canonical(name: str) -> str:
    """Fold a type or predicate name to snake_case, the same rule `OntologyKind.canonical` applies.

    Inlined rather than imported from the app so this migration stays a frozen record of the
    transform it ran, immune to a later change in the model's own helper.

    name: a raw catalog name, PascalCase or spaced or already canonical.
    """
    return inflection.parameterize(inflection.underscore(name), separator="_")


def rename_catalog(connection: sa.Connection, catalog: str, child: str, column: str) -> None:
    """Rename every row of one catalog to its canonical name, repointing its FK children.

    catalog: the ontology table to canonicalize, `entity_kind` or `relation_kind`.
    child: the table whose column foreign-keys against `catalog.name`.
    column: that foreign-key column, `type` or `predicate`.
    """
    rows = connection.execute(
        sa.text(f"SELECT name, description, domain, structural FROM {catalog}")  # noqa: S608
    ).all()
    for name, description, domain, structural in rows:
        canon = canonical(name)
        if canon == name:
            continue
        connection.execute(
            sa.text(
                f"INSERT INTO {catalog} (name, description, domain, structural) "  # noqa: S608
                "VALUES (:canon, :description, :domain, :structural) ON CONFLICT (name) DO NOTHING"
            ),
            {
                "canon": canon,
                "description": description,
                "domain": domain,
                "structural": structural,
            },
        )
        connection.execute(
            sa.text(f"UPDATE {child} SET {column} = :canon WHERE {column} = :old"),  # noqa: S608
            {"canon": canon, "old": name},
        )
        connection.execute(
            sa.text(f"DELETE FROM {catalog} WHERE name = :old"),  # noqa: S608
            {"old": name},
        )


def upgrade() -> None:
    connection = op.get_bind()
    rename_catalog(connection, "entity_kind", "entity_content", "type")
    rename_catalog(connection, "relation_kind", "fact_content", "predicate")


def downgrade() -> None:
    raise NotImplementedError("snake_case canonicalization is lossy and has no faithful reverse")
