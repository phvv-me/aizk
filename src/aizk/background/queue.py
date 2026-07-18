import abc
from functools import cached_property, partial
from types import TracebackType
from typing import ClassVar, Self, cast

import asyncpg
from asyncpg.exceptions import DuplicateFunctionError, DuplicateObjectError, DuplicateTableError
from loguru import logger
from patos import FrozenModel
from pgqueuer import PgQueuer, Queries
from pgqueuer.db import AsyncpgDriver
from pgqueuer.errors import DuplicateJobError
from pgqueuer.executors import DatabaseRetryEntrypointExecutor
from pgqueuer.models import Job
from pydantic import PrivateAttr
from sqlalchemy import Column as SAColumn
from sqlalchemy import Index, MetaData, String, Table, and_
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.schema import CreateIndex, DropIndex

from ..config import settings
from ..store.ddl import Grant, GrantTarget, postgresql_sql
from .enum import QueueStatus


class QueuePayload(FrozenModel):
    """Typed JSON payload persisted by PgQueuer."""

    def encode(self) -> bytes:
        """Serialize this payload for PgQueuer storage."""
        return self.model_dump_json().encode()

    @classmethod
    def decode(cls, payload: bytes) -> Self:
        """Validate a PgQueuer payload into its declared type."""
        return cls.model_validate_json(payload)


class QueueSchema(FrozenModel):
    """Names discovered from PgQueuer's own database settings."""

    queue: str
    log: str
    statistics: str
    schedules: str
    status_type: str

    @property
    def tables(self) -> tuple[str, ...]:
        """Return every PgQueuer table installed for this namespace."""
        return self.queue, self.log, self.statistics, self.schedules

    @property
    def sequences(self) -> tuple[str, ...]:
        """Return the identity sequence created for each PgQueuer table."""
        return tuple(f"{table}_id_seq" for table in self.tables)


class Queue(FrozenModel):
    """Typed application boundary over one PgQueuer connection."""

    dsn: str
    _connection: asyncpg.Connection | None = PrivateAttr(default=None)

    async def __aenter__(self) -> Self:
        """Open the queue connection."""
        assert self.__pydantic_private__ is not None
        self.__pydantic_private__["_connection"] = await asyncpg.connect(self.dsn)
        self.__dict__.pop("queries", None)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the queue connection."""
        connection = self.connection
        assert self.__pydantic_private__ is not None
        self.__pydantic_private__["_connection"] = None
        self.__dict__.pop("queries", None)
        await connection.close()

    @property
    def connection(self) -> asyncpg.Connection:
        """Return the active queue connection."""
        if self._connection is None:
            raise RuntimeError("queue is not open")
        return self._connection

    @cached_property
    def queries(self) -> Queries:
        """Build the query facade when the first queue operation needs it."""
        return Queries(AsyncpgDriver(self.connection))

    def worker(self) -> PgQueuer:
        """Build a PgQueuer worker over this queue connection."""
        return PgQueuer.from_asyncpg_connection(self.connection)

    async def enqueue[PayloadT: QueuePayload](
        self,
        job: type[QueueJob[PayloadT]],
        payload: PayloadT,
        dedupe_key: str,
    ) -> bool:
        """Persist one typed job and report whether deduplication admitted it."""
        try:
            await self.queries.enqueue(
                job.entrypoint,
                payload.encode(),
                priority=job.priority,
                dedupe_key=dedupe_key,
            )
        except DuplicateJobError:
            return False
        return True

    async def requeue_failed[PayloadT: QueuePayload](
        self, job: type[QueueJob[PayloadT]], limit: int = 100
    ) -> int:
        """Requeue up to `limit` retained failures for one typed job.

        The entrypoint filter runs in the query itself, so failures of other job types
        can never occupy the window and hide this job's retained failures.
        """
        table = self.queries.qbe.settings.queue_table
        rows = await self.queries.driver.fetch(
            f"SELECT id FROM {table} WHERE status = 'failed' AND entrypoint = $1"
            " ORDER BY created DESC LIMIT $2",
            job.entrypoint,
            limit,
        )
        ids = [row["id"] for row in rows]
        if ids:
            await self.queries.requeue_jobs(ids)
        return len(ids)

    async def install(self) -> QueueSchema:
        """Install or upgrade the PgQueuer schema and report its configured object names."""
        try:
            await self.queries.install()
        except DuplicateFunctionError, DuplicateObjectError, DuplicateTableError:
            await self.queries.upgrade()
            logger.info("pgqueuer schema upgraded")
        names = self.queries.qbe.settings
        return QueueSchema(
            queue=names.queue_table,
            log=names.queue_table_log,
            statistics=names.statistics_table,
            schedules=names.schedules_table,
            status_type=names.queue_status_type,
        )


class QueueJob[PayloadT: QueuePayload](abc.ABC):
    """Declarative PgQueuer job with typed payload and uniform recovery policy."""

    entrypoint: ClassVar[str]
    payload_type: ClassVar[type[QueuePayload]]
    priority: ClassVar[int] = 0
    concurrency_limit: ClassVar[int] = 0
    max_attempts: ClassVar[int] = 5

    @abc.abstractmethod
    async def handle(self, payload: PayloadT) -> None:
        """Execute one validated payload."""

    async def consume(self, job: Job) -> None:
        """Decode one raw PgQueuer job and execute its typed body."""
        assert job.payload is not None
        await self.handle(cast(PayloadT, self.payload_type.decode(job.payload)))

    def bind(self, worker: PgQueuer) -> None:
        """Register this job with retries and retained terminal failures."""
        worker.entrypoint(
            self.entrypoint,
            concurrency_limit=self.concurrency_limit,
            on_failure="hold",
            executor_factory=partial(
                DatabaseRetryEntrypointExecutor,
                max_attempts=self.max_attempts,
            ),
        )(self.consume)

    async def enqueue(
        self,
        queue: Queue,
        payload: PayloadT,
        dedupe_key: str,
    ) -> bool:
        """Enqueue this typed job once for its deduplication key."""
        return await queue.enqueue(type(self), payload, dedupe_key)


async def grant_queue_access(
    connection: asyncpg.Connection,
    role: str,
    schema: QueueSchema,
) -> None:
    """Grant the app role only the objects PgQueuer reports installing."""
    grants = (
        *(
            Grant(
                GrantTarget.table,
                table,
                role,
                ("SELECT", "INSERT", "UPDATE", "DELETE"),
            )
            for table in schema.tables
        ),
        *(
            Grant(GrantTarget.sequence, sequence, role, ("USAGE", "SELECT"))
            for sequence in schema.sequences
        ),
    )
    for grant in grants:
        await connection.execute(postgresql_sql(grant))


async def install_queue_schema() -> None:
    """Install the pgqueuer tables and grant the app role access, run as the owner.

    A session advisory lock serializes concurrent service startups. A matching
    `pg_indexes` definition makes steady-state startup a no-op for the live dedupe
    index; changing the declared definition makes the check fail and rebuilds it
    under the same lock during deploys. Grants are refreshed before the lock releases
    with the connection.
    """
    async with Queue(dsn=settings.admin_asyncpg_dsn) as queue:
        await queue.connection.execute(
            "SELECT pg_advisory_lock(hashtextextended('aizk.install_queue_schema', 0))"
        )
        schema = await queue.install()
        table = Table(
            schema.queue,
            MetaData(),
            SAColumn("dedupe_key", String),
            SAColumn(
                "status",
                ENUM(QueueStatus, name=schema.status_type, create_type=False),
            ),
        )
        dedupe = Index(
            f"{schema.queue}_unique_dedupe_key",
            table.c.dedupe_key,
            unique=True,
            postgresql_where=and_(
                table.c.dedupe_key.is_not(None),
                table.c.status.in_((QueueStatus.queued, QueueStatus.picked, QueueStatus.failed)),
            ),
        )
        definition_current = await queue.connection.fetchval(
            """
            SELECT
                state.indisunique
                AND state.indisvalid
                AND state.indisready
                AND state.indnkeyatts = 1
                AND state.indnatts = 1
                AND pg_get_indexdef(state.indexrelid, 1, true) = 'dedupe_key'
                AND position(' USING btree ' IN indexes.indexdef) > 0
                AND regexp_replace(
                    regexp_replace(
                        pg_get_expr(state.indpred, state.indrelid, true),
                        '::[[:alnum:]_."$]+',
                        '',
                        'g'
                    ),
                    '[[:space:]()]',
                    '',
                    'g'
                ) = 'dedupe_keyISNOTNULLANDstatus=ANYARRAY[''queued'',''picked'',''failed'']'
            FROM pg_indexes AS indexes
            JOIN pg_class AS index_relation
              ON index_relation.oid = to_regclass(
                  quote_ident(indexes.schemaname) || '.' || quote_ident(indexes.indexname)
              )
            JOIN pg_index AS state ON state.indexrelid = index_relation.oid
            WHERE indexes.schemaname = current_schema()
              AND indexes.tablename = $1
              AND indexes.indexname = $2
            """,
            schema.queue,
            dedupe.name,
        )
        if definition_current is not True:
            await queue.connection.execute(postgresql_sql(DropIndex(dedupe, if_exists=True)))
            await queue.connection.execute(postgresql_sql(CreateIndex(dedupe)))
        await grant_queue_access(queue.connection, settings.app_role, schema)
    logger.info("pgqueuer schema installed and granted to {}", settings.app_role)
