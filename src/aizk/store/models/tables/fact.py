import uuid
from datetime import UTC, datetime

import rls
from sqlalchemy import (
    Boolean,
    Column,
    ColumnElement,
    DateTime,
    Index,
    Table,
    Text,
    and_,
    func,
    or_,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, Range
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declared_attr
from sqlalchemy.sql.functions import GenericFunction
from sqlmodel import Field

from ...engine import session
from ...mixins import Embedded, Id, Scoped, TableBase
from ...mixins.scoped import ScopeLattice
from .entity import content_policies


def _curation_admin_policies(table: Table) -> list[rls.Policy]:
    """Supplemental SELECT, UPDATE, and DELETE policies letting an admin govern `fact_claim`'s
    curated rows, `FactClaim`'s own addition to `Scoped`'s default set.

    Postgres combines multiple PERMISSIVE policies for one command with OR, so these ride alongside
    `Scoped`'s ordinary scope policies as an additive escape rather than replacing them. A curated
    group's own admin member keeps reaching its rows through the ordinary scope policies, and a
    server admin, or a user separately administering every group a row's set names, reaches it
    through these instead. A curated group's canon is centrally governed, so a server admin may
    read and review its rows even without a membership row anywhere in the set, the standing
    `auth.groups.require_group_admin` already vouches for at the application layer before any of
    these rows are ever touched. INSERT carries no admin policy, since review only approves or
    rejects a claim a member already wrote, never mints one. Lives beside `FactClaim`, its only
    caller, rather than on `ScopeLattice` itself, since no other table in the schema needs this
    escape.

    table: `FactClaim.__table__`, read for its own `owner_id`/`scopes` columns through
        `ScopeLattice`.
    """
    lattice = ScopeLattice(table)
    admin = and_(
        func.cardinality(lattice.scopes) > 0,
        lattice.scopes.contained_by(ScopeLattice.curated_group_ids()),
        or_(lattice.scopes.contained_by(ScopeLattice.admin_group_ids()), ScopeLattice.is_admin()),
    )
    return [
        rls.Policy(name="curation_admin_read", command=rls.Command.select, using=admin),
        rls.Policy(
            name="curation_admin_update", command=rls.Command.update, using=admin, check=admin
        ),
        rls.Policy(name="curation_admin_delete", command=rls.Command.delete, using=admin),
    ]


# bumps recency and frequency on every latest claim whose fact content's statement recall just
# surfaced, scoped to the caller by the row level security app.uid already on the session. Raw
# text() UPDATE rather than Core update(), since the match needs a subquery onto fact_content
# (statement lives on the content the claim stakes, not on the claim), and that subquery reads
# through content's own visible-through-a-claim policy so it never widens what the session may see.
RECORD_ACCESS_STATEMENT = text("""
UPDATE fact_claim
SET last_accessed = now(), access_count = access_count + 1
WHERE upper_inf(recorded)
  AND content_id IN (
      SELECT id FROM fact_content
      WHERE statement = ANY(CAST(:statements AS text[]))
  )
""")

# archives every visible latest claim whose exponential-decay relevance falls under the floor, a
# set-based UPDATE rather than scoring each claim in Python. The live predicate is hand-listed
# (open `recorded`, open `valid`) since a raw UPDATE sits outside the do_orm_execute listener, and
# the power/extract expression mirrors `FactClaim.relevance`. Closes `recorded`'s upper bound
# rather than an is_latest flag, so a decayed claim's history reads like a superseded one.
DECAY_SQL = text("""
UPDATE fact_claim
SET recorded = tstzrange(lower(recorded), CAST(:now AS timestamptz)),
    attributes = attributes || jsonb_build_object('decayed', CAST(:now_iso AS text))
WHERE upper_inf(recorded)
  AND (valid IS NULL OR valid @> CAST(:now AS timestamptz))
  AND power(0.5::float8, extract(epoch FROM
        (CAST(:now AS timestamptz) - coalesce(last_accessed, lower(recorded))))
        / 86400.0 / :half_life_days
     ) * (1 + access_count) < :floor
RETURNING id
""")


# retracts every live claim derived from a given set of documents, closing `recorded`'s upper
# bound exactly as DECAY_SQL does, so a forgotten claim reads like a superseded one in history and
# an as-of query still sees it. Marks the row forgotten rather than decayed. This is the provenance
# sweep the roadmap's `forget` describes, source_chunk_id back to the documents whose facts should
# never have been mined, run without deleting anything so an over-eager forget is undoable and the
# documents themselves stay indexable for retrieval.
FORGET_SQL = text("""
UPDATE fact_claim
SET recorded = tstzrange(lower(recorded), CAST(:now AS timestamptz)),
    attributes = attributes || jsonb_build_object('forgotten', CAST(:now_iso AS text))
WHERE upper_inf(recorded)
  AND source_chunk_id IN (SELECT id FROM chunk WHERE document_id = ANY(:document_ids))
RETURNING id
""")


class upper_inf(GenericFunction):
    """Typed Core registration of Postgres's `upper_inf(anyrange)`.

    Every other `func.upper_inf(...)` call in the codebase resolves through this one registration
    once `FactClaim` is imported, so the function reads as `Boolean` in a typed `where()` clause
    rather than falling back to SQLAlchemy's untyped `NULLTYPE`.
    """

    type = Boolean()
    name = "upper_inf"
    inherit_cache = True


class FactClaim(Id, Scoped, TableBase, table=True):
    """A container's bi-temporal stake in an edge, the union that lets a fact belong to A or B.

    World-time validity, review state, and access recency live here rather than on `FactContent`,
    since they are inherently a container's own claim on the shared structure, never the structure
    itself. Consolidation supersedes an old claim by closing its `recorded` upper bound and
    inserting a new one with a fresh open `recorded`, so history is never overwritten, and the
    partial unique index below enforces at most one *live* claim per container per content.

    Declared before `FactContent` in this file, not just after, since `store.rls.register`'s
    mapper-construction hook calls `FactContent.__rls_policies__` synchronously the moment
    `FactContent`'s own class statement finishes, before the rest of the module runs, so it can
    only resolve a bare `FactClaim` name already bound in module globals by then, not one defined
    later in the same file.

    id: uuid7 claim identity.
    content_id: the fact content this claim stakes, cascading on delete.
    owner_id: user that holds this claim, enforced by row level security.
    scopes: group set this claim is shared with, an implicit intersection when it names more than
        one, empty when private to the owner.
    valid: world-time range when the statement holds, null when undated. An open upper bound means
        the statement still holds, and a null range means no claim is made about its world-time
        extent at all.
    recorded: transaction-time range this container has known this version under, lower is the
        write time and an open upper bound means this is the live version. Consolidation closes it
        to retire a version rather than deleting the row.
    reviewed_at: when this claim cleared curated-group review and joined the visible canon, stamped
        immediately on write for a private scope or an uncurated group, null while it sits pending
        in a curated group's review queue, invisible to everyone but its author until a group
        admin approves it through `approve_facts`.
    last_accessed: transaction time recall last surfaced this claim, null until first recalled, the
        recency half of the decay relevance score.
    access_count: how many times recall has surfaced this claim, the frequency half of decay.
    attributes: free-form structured detail extracted alongside this claim, also where the decay
        pass stamps its archived marker so a forgotten claim is told apart from a superseded one.
    source_chunk_id: chunk the fact was extracted from, null when the chunk is gone.
    promoted_from: the claim this one was promoted from, null for an ordinary write, the provenance
        `graph.promote` stamps on the fresh claim it mints in the wider target scope.
    """

    content_id: uuid.UUID = Field(
        foreign_key="fact_content.id", ondelete="CASCADE", nullable=False, index=True
    )
    valid: Range[datetime] | None = Field(default=None, sa_column=Column(TSTZRANGE))
    recorded: Range[datetime] = Field(
        default=None,
        sa_column=Column(
            TSTZRANGE, nullable=False, server_default=text("tstzrange(now(), NULL, '[)')")
        ),
    )
    reviewed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    last_accessed: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    access_count: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    attributes: dict = Field(
        default_factory=dict, sa_column_kwargs={"server_default": "{}"}, sa_type=JSONB
    )
    # indexed: pending_chunks's NOT EXISTS anti-join reads every chunk against this column every
    # build_graph and enqueue_pending run; EXPLAIN against a seeded corpus showed the unindexed
    # join materializing the whole claim table per candidate chunk, a chunks-times-claims cost
    source_chunk_id: uuid.UUID | None = Field(
        default=None, foreign_key="chunk.id", ondelete="SET NULL", index=True
    )
    promoted_from: uuid.UUID | None = Field(
        default=None, foreign_key="fact_claim.id", ondelete="SET NULL", index=True
    )

    @classmethod
    def __rls_policies__(cls) -> list[rls.Policy]:
        """`Scoped`'s default policies plus the curation-admin escape, `fact_claim`'s own set.

        Only a curated group's canon needs a server-wide admin's cross-tenant reach, so
        `_curation_admin_policies` rides alongside the ordinary scope policies as additive
        PERMISSIVE policies Postgres already ORs together per command. Composes on top of
        `Scoped`'s own default set (`super().__rls_policies__()`) rather than rebuilding it, the
        same `*super().__table_args__` composition every `__table_args__` override already uses.
        """
        return [*super().__rls_policies__(), *_curation_admin_policies(cls.__table__)]

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index, ...]:
        # GiST for `valid` and `recorded`, the containment (`@>`) operator the as-of gate filters
        # on. `upper_inf` is a function over the range, not an indexable range_ops operator, so
        # neither plain index makes the live gate sargable alone; EXPLAIN confirmed a bare
        # `upper_inf(recorded)` predicate otherwise falls back to a full table scan.
        # ix_fact_claim_live is `valid` scoped by its own `upper_inf(recorded)` partial predicate,
        # so a query filtering both matches the partial index and scans only the live set.
        # uq_fact_claim_live is the one-live-claim-per-container-per-content moat: a partial unique
        # index since Postgres allows no WHERE clause on a table constraint, only on an index; a
        # `uuid[]` carries no NULL to fold, an empty array is its own ordinary, comparable value,
        # so this needs no NULLS NOT DISTINCT the way the old scalar `scope` column once did.
        # ix_fact_claim_scopes is a GIN index over the scope-set array, fact_claim is a hot table
        # for the containment reads `mixins.scoped.ScopeLattice`'s policies run on every visible
        # row.
        # No `*super().__table_args__` here: unlike `FactContent`, a claim mixes in no `Embedded`
        # and declares no embedding column to extend.
        return (
            Index("ix_fact_claim_valid", "valid", postgresql_using="gist"),
            Index("ix_fact_claim_recorded", "recorded", postgresql_using="gist"),
            Index(
                "ix_fact_claim_live",
                "valid",
                postgresql_using="gist",
                postgresql_where=text("upper_inf(recorded)"),
            ),
            Index(
                "uq_fact_claim_live",
                "content_id",
                "owner_id",
                "scopes",
                unique=True,
                postgresql_where=text("upper_inf(recorded)"),
            ),
            Index("ix_fact_claim_scopes", "scopes", postgresql_using="gin"),
        )

    @classmethod
    def _is_current_predicate(cls) -> ColumnElement[bool]:
        """SQL form of `is_current`, an open `recorded` upper bound and an open `valid` window.

        The single definition of "current", shared by `is_current_expression`, the do_orm_execute
        loader-criteria listener, and `visible_at`'s live branch. Reached through `cls.__table__.c`
        rather than the mapped `cls.recorded`/`cls.valid` attributes, since a plain SQLModel
        `Field` column carries no `Mapped[...]` wrapper, so a class-level attribute access on it
        types as the bare pydantic field rather than the `InstrumentedAttribute` that carries
        `.is_`/`.contains`. The underlying `Column` always has that typed API, mapped or not.
        """
        columns = cls.__table__.c
        return and_(
            func.upper_inf(columns.recorded),
            or_(columns.valid.is_(None), columns.valid.contains(func.now())),
        )

    @hybrid_property
    def is_current(self) -> bool:
        """Whether this claim is the live version with an open valid-time window right now.

        Mirrors `is_current_expression` in Python. `recorded` is still open and, when `valid` is
        set, now falls inside it.
        """
        now = datetime.now(UTC)
        return bool(self.recorded.upper_inf and (self.valid is None or now in self.valid))

    @is_current.inplace.expression
    @classmethod
    def is_current_expression(cls) -> ColumnElement[bool]:
        """SQL form of `is_current`, delegating to the shared `_is_current_predicate`.

        A hybrid classmethod resolves against whichever entity SQLAlchemy passes, the bare
        `FactClaim` class or an alias of it, so an aliased claim in a relationship load is gated
        on its own columns without a separate helper to thread the entity through.
        """
        return cls._is_current_predicate()

    @hybrid_property
    def created_at(self) -> datetime:
        """When this claim was first recorded, an ergonomic mirror of `recorded`'s lower bound.

        Every other table's own `created_at` is a first-seen timestamp. A claim already carries
        that moment as the lower bound of its bi-temporal `recorded` range, so this adds no column
        of its own, just the same friendlier name the rest of the schema already uses.
        """
        assert self.recorded.lower is not None, "a claim's recorded range always has a lower bound"
        return self.recorded.lower

    @created_at.inplace.expression
    @classmethod
    def created_at_expression(cls) -> ColumnElement[datetime]:
        """SQL form of `created_at`, Postgres's own `lower(recorded)` range function."""
        return func.lower(cls.__table__.c.recorded)

    @staticmethod
    def visible_at(as_of: datetime | None) -> list[ColumnElement[bool]]:
        """Temporal predicates keeping claims to the version current now or as it stood at as_of.

        Embedding cosine cannot tell a contradicted claim from a duplicate, so validity is enforced
        structurally on the read path. With no as_of this is just `is_current`. With an as_of it
        replays history, matching the claim whose `recorded` range contained that instant and
        whose `valid` window, if any, also contained it. Listed explicitly only on this replay
        path, since the live branch normally applies through the do_orm_execute listener instead.

        as_of: world-time to read the graph at, the live latest graph when null.
        """
        if as_of is None:
            return [FactClaim._is_current_predicate()]
        columns = FactClaim.__table__.c
        return [
            or_(columns.valid.is_(None), columns.valid.contains(as_of)),
            columns.recorded.contains(as_of),
        ]

    def relevance(self, now: datetime, half_life_days: float) -> float:
        """Score this claim by how recently and often recall has reached for it.

        Decays exponentially from the claim's last access against the half-life so an untouched
        claim fades while a recently surfaced one stays near full relevance, then lifts the score
        by how often recall has returned it so a used claim resists forgetting. A claim never
        accessed is scored from when it entered memory, the lower bound of `recorded`. This is the
        Python-side mirror of the `power`/`extract` expression `graph.decay.DECAY_SQL` scores the
        same way in one UPDATE.

        now: the moment decay runs, the reference the age is measured against.
        half_life_days: age in days at which an unaccessed claim's relevance halves.
        """
        reference = self.last_accessed or self.recorded.lower
        # `Range.lower` types as Optional for the general case of an unbounded range, but a
        # FactClaim's `recorded` range always carries a concrete write-time lower bound in this
        # domain (`FactClaim.__table_args__` server-defaults it to `tstzrange(now(), NULL, '[)')`);
        # the assert narrows the union to the invariant this domain actually holds.
        assert reference is not None, "a claim's recorded range always has a lower bound"
        elapsed = now - reference
        age_days = elapsed.total_seconds() / 86400.0
        recency = 0.5 ** (age_days / half_life_days)
        return recency * (1 + self.access_count)

    @classmethod
    async def record_access(cls, statements: list[str]) -> None:
        """Bump the access recency and count of the latest claims recall just surfaced.

        Sets last_accessed to now and increments access_count for every latest claim whose fact
        content statement is in the surfaced set, so the decay pass can tell a claim memory keeps
        reaching for from one it has not touched in months. Runs on the same user-scoped
        session recall already holds open.

        statements: the fact statements recall returned in this call.
        """
        if not statements:
            return
        await session().execute(RECORD_ACCESS_STATEMENT, {"statements": statements})

    @classmethod
    async def archive_stale(cls, half_life_days: float, floor: float) -> list[uuid.UUID]:
        """Archive the stale, rarely accessed latest claims, return the archived claim ids.

        Scores each visible latest claim by an exponential decay of its age against
        half_life_days, lifted by how often and how recently recall has reached for it, then
        archives the claims that fall below the relevance floor by closing `recorded` and marking
        the row decayed in its attributes. Nothing is deleted, so an archived claim stays in
        history and an as-of query still sees it, it only leaves the live graph default recall
        reads.

        half_life_days: age in days at which an unaccessed claim's relevance halves.
        floor: relevance floor a claim must clear to stay in the live graph.
        """
        now = datetime.now(UTC)
        result = await session().execute(
            DECAY_SQL,
            {
                "now": now,
                "now_iso": now.isoformat(),
                "half_life_days": half_life_days,
                "floor": floor,
            },
        )
        return list(result.scalars().all())

    @classmethod
    async def forget_from_documents(cls, document_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Retract every live claim derived from the given documents, return the retracted ids.

        Closes `recorded` on each live claim whose source chunk belongs to one of the documents,
        the provenance sweep `forget` runs when a source should never have been mined for facts.
        The documents stay stored and indexable for retrieval, only their derived claims leave the
        live graph, and nothing is deleted so the claims keep their history and an over-eager
        forget is undoable through an as-of read.

        document_ids: the source documents whose derived claims are retracted.
        """
        now = datetime.now(UTC)
        result = await session().execute(
            FORGET_SQL, {"now": now, "now_iso": now.isoformat(), "document_ids": document_ids}
        )
        return list(result.scalars().all())


class FactContent(Id, Embedded, TableBase, table=True):
    """The immutable, deduplicated structure of a graph edge, content-addressed and tenant-free.

    The triple, its statement, and its embedding are the structural knowledge two containers share
    when they independently extract the identical fact. Two owners writing the same subject,
    predicate, object, and statement land one content row, each holding their own bi-temporal
    `FactClaim` on it. All curation, decay, and access-tracking state lives on the claim instead,
    since it is inherently per-container, never structural.

    id: content-addressed identity from uuid5 over the normalized triple and statement.
    subject_id: entity content the fact is about, cascading on delete.
    object_id: entity content the fact points to, null for unary facts, cascading on delete.
    predicate: relation type, foreign-keyed against the live `relation_kind` catalog, the wall
        that keeps a stray or off-vocabulary predicate from ever reaching a row regardless of
        what path wrote it.
    statement: self-contained natural-language rendering of the fact.
    embedding: halfvec dense vector of the statement, null until embedded, stored once regardless
        of how many containers claim this content.
    """

    subject_id: uuid.UUID = Field(
        foreign_key="entity_content.id", ondelete="CASCADE", nullable=False, index=True
    )
    object_id: uuid.UUID | None = Field(
        default=None, foreign_key="entity_content.id", ondelete="CASCADE", index=True
    )
    predicate: str = Field(sa_type=Text, foreign_key="relation_kind.name")
    statement: str = Field(sa_type=Text)

    @classmethod
    def __rls_policies__(cls) -> list[rls.Policy]:
        """Visible through a `fact_claim`, freely mintable, immutable, admin-only to delete."""
        return content_policies(FactClaim)
