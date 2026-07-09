import uuid
from datetime import UTC, datetime

import rls
from sqlalchemy import (
    Boolean,
    Column,
    ColumnElement,
    DateTime,
    Float,
    Index,
    Text,
    and_,
    cast,
    extract,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, Range
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declared_attr
from sqlalchemy.sql.functions import GenericFunction
from sqlmodel import Field

from ...engine import session
from ...mixins import Embedded, Id, Scoped, TableBase
from .chunk import Chunk
from .entity import content_policies


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

    World-time validity and access recency live here rather than on `FactContent`,
    since they are inherently a container's own claim on the shared structure, never the structure
    itself. Consolidation supersedes an old claim by closing its `recorded` upper bound and
    inserting a new one with a fresh open `recorded`, so history is never overwritten, and the
    partial unique index below enforces at most one *live* claim per container per content.

    Declared before `FactContent` in this file so the bare `FactClaim` name its read-through-claim
    policy references is bound in module globals when `rls.register` reads
    `FactContent.__rls_policies__`. That read is a backfill `rls.register` runs after `aizk.store`
    has imported every model, so any in-file order would in fact resolve, but keeping the claim
    first states the dependency plainly.

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
        """`Scoped`'s default scope policies, `fact_claim`'s own set."""
        return super().__rls_policies__()

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
        Python-side mirror of the `power`/`extract` expression `archive_stale` scores the same way
        in one set-based UPDATE.

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
        columns = cls.__table__.c
        content = FactContent.__table__.c
        # the match needs a subquery onto `fact_content`, since `statement` lives on the content
        # the claim stakes, not on the claim, and that subquery reads through content's own
        # visible-through-a-claim policy so it never widens what this user-scoped session may see.
        # `synchronize_session=False` keeps this a single set-based UPDATE with no ORM fetch-back.
        await session().execute(
            update(cls)
            .where(
                func.upper_inf(columns.recorded),
                columns.content_id.in_(
                    select(content.id).where(content.statement.in_(statements))
                ),
            )
            .values(last_accessed=func.now(), access_count=columns.access_count + 1)
            .execution_options(synchronize_session=False)
        )

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
        now_ts = cast(now, DateTime(timezone=True))
        columns = cls.__table__.c
        # the `power`/`extract` relevance mirrors `relevance`, scored set-based here: half-lives
        # elapsed since the last touch (or the write time for an untouched claim), lifted by the
        # access count. The live gate is hand-listed since an ORM UPDATE sits outside the
        # do_orm_execute select listener, and `0.5` is cast to float8 to score in double precision.
        half_lives = (
            extract(
                "epoch",
                now_ts - func.coalesce(columns.last_accessed, func.lower(columns.recorded)),
            )
            / 86400.0
            / half_life_days
        )
        relevance = func.power(cast(0.5, Float), half_lives) * (1 + columns.access_count)
        result = await session().execute(
            update(cls)
            .where(
                func.upper_inf(columns.recorded),
                or_(columns.valid.is_(None), columns.valid.contains(now_ts)),
                relevance < floor,
            )
            .values(
                recorded=func.tstzrange(func.lower(columns.recorded), now_ts),
                attributes=columns.attributes.op("||")(
                    func.jsonb_build_object("decayed", cast(now.isoformat(), Text))
                ),
            )
            .returning(columns.id)
            .execution_options(synchronize_session=False)
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
        now_ts = cast(now, DateTime(timezone=True))
        columns = cls.__table__.c
        chunks = Chunk.__table__.c
        # closes `recorded` exactly as `archive_stale` does, walking `source_chunk_id` back to the
        # forgotten documents. Marks the row forgotten rather than decayed, and deletes nothing so
        # an over-eager forget stays undoable through an as-of read.
        result = await session().execute(
            update(cls)
            .where(
                func.upper_inf(columns.recorded),
                columns.source_chunk_id.in_(
                    select(chunks.id).where(chunks.document_id.in_(document_ids))
                ),
            )
            .values(
                recorded=func.tstzrange(func.lower(columns.recorded), now_ts),
                attributes=columns.attributes.op("||")(
                    func.jsonb_build_object("forgotten", cast(now.isoformat(), Text))
                ),
            )
            .returning(columns.id)
            .execution_options(synchronize_session=False)
        )
        return list(result.scalars().all())


class FactContent(Id, Embedded, TableBase, table=True):
    """The immutable, deduplicated structure of a graph edge, content-addressed and tenant-free.

    The triple, its statement, and its embedding are the structural knowledge two containers share
    when they independently extract the identical fact. Two owners writing the same subject,
    predicate, object, and statement land one content row, each holding their own bi-temporal
    `FactClaim` on it. All decay and access-tracking state lives on the claim instead,
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
        """Visible through a `fact_claim`, freely mintable, and otherwise immutable."""
        return content_policies(FactClaim)
