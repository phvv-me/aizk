import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from aizk.config import settings
from aizk.extract.models import ConsolidationVerdict, Extraction, TimestampResolution
from aizk.graph.models import (
    CommunitySummary,
    CurationReview,
    InsightReport,
    ProfileReport,
    RaptorReport,
)
from aizk.store import Group, Principal, acting_as, async_session, system_session

# the area probes Postgres once at import so the DB-backed graph tests deselect cleanly when the
# database is absent, mirroring the shared DB_UP gate the rest of the suite reads.
_db = urlsplit(settings.database_url)


def port_open(host: str | None, port: int | None, timeout: float = 0.5) -> bool:
    """Whether a TCP connection to host and port succeeds within timeout.

    host: target hostname, unreachable when missing.
    port: target port, unreachable when missing.
    timeout: connection deadline in seconds.
    """
    if host is None or port is None:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


DB_UP = port_open(_db.hostname, _db.port)


def default_response(schema: type[BaseModel]) -> BaseModel:
    """A minimal valid instance of one extractor or summarizer schema, the fake LLM's fallback.

    schema: the response model the seam asked the LLM for.
    """
    defaults: dict[type[BaseModel], BaseModel] = {
        Extraction: Extraction(entities=[], facts=[]),
        TimestampResolution: TimestampResolution(timestamps=[]),
        ConsolidationVerdict: ConsolidationVerdict(action="ADD"),
        CommunitySummary: CommunitySummary(label="cluster theme", summary="a grounded paragraph"),
        ProfileReport: ProfileReport(summary="a static and dynamic paragraph"),
        RaptorReport: RaptorReport(label="broad theme", summary="a rolled-up paragraph"),
        InsightReport: InsightReport(observations=[]),
        CurationReview: CurationReview(verdicts=[]),
    }
    return defaults[schema]


@dataclass
class FakeMessage:
    """The `.choices[0].message` shape `structured` reads its `.parsed` schema instance off of.

    parsed: the canned or default model instance the fake turn resolves to.
    """

    parsed: BaseModel


@dataclass
class FakeChoice:
    """The `.choices[0]` shape wrapping the fake message, mirroring `ParsedChoice`.

    message: the fake message carrying the parsed schema instance.
    """

    message: FakeMessage


@dataclass
class FakeParsedCompletion:
    """A minimal stand-in for `openai.types.chat.ParsedChatCompletion`, since `.choices[0].message.
    parsed` is the only path `structured` reads off the real response.

    choices: always the one fake choice a non-streaming, `n=1` chat completion carries.
    """

    choices: list[FakeChoice]


class FakeCompletions:
    """A recording completions stand-in dispatching on the requested response_format schema.

    It returns the canned instance a test registered for a schema, or a minimal valid default, so
    every summarizer and extractor that flows through `structured` runs without the local model.
    This replaces the one external LLM process at its seam, never any of our own classes.

    responses: per-schema overrides the test installs, falling back to a minimal valid default.
    calls: every turn's kwargs, normalized back to `response_model`/`messages` keys so a test
        keeps reading the shape `structured` used to send under instructor's `create`, for
        asserting the prompt shape and call count.
    """

    def __init__(self) -> None:
        self.responses: dict[type[BaseModel], BaseModel] = {}
        self.calls: list[dict[str, object]] = []

    async def parse(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: type[BaseModel],
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> FakeParsedCompletion:
        """Record the turn and return the canned or default model for its response_format schema.

        model: chat model id the seam sent.
        messages: the system-then-user message pair the seam assembled.
        response_format: schema the caller asked the structured turn to validate against.
        temperature: sampling temperature, accepted and ignored.
        timeout: per-call ceiling, accepted and ignored.
        max_tokens: output token cap, accepted and ignored.
        """
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_model": response_format,
                "temperature": temperature,
                "timeout": timeout,
                "max_tokens": max_tokens,
            }
        )
        parsed = self.responses.get(response_format) or default_response(response_format)
        return FakeParsedCompletion(choices=[FakeChoice(FakeMessage(parsed))])


class FakeChat:
    """The chat namespace wrapping the fake completions.

    completions: the fake completions endpoint.
    """

    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeLLM:
    """An AsyncOpenAI client stand-in exposing only the chat.completions.parse path the seam uses.

    completions: the recording completions the chat namespace exposes.
    """

    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = FakeChat(self.completions)


async def add_principals(*ids: uuid.UUID) -> None:
    """Seed one or more principals on the non-scoped identity table.

    ids: principal ids to insert.
    """
    async with async_session()() as session, session.begin():
        await session.execute(
            text("INSERT INTO principal (id) VALUES (:id)"),
            [{"id": pid} for pid in ids],
        )


async def drop_principals(*ids: uuid.UUID) -> None:
    """Remove the seeded principals once their owned rows are gone.

    ids: principal ids to delete.
    """
    async with async_session()() as session, session.begin():
        await session.execute(
            text("DELETE FROM principal WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": list(ids)},
        )


async def purge_owner(owner: uuid.UUID) -> None:
    """Delete every scoped row an owner wrote, in foreign-key order, as that owner.

    Claims are the owner's own rows and delete straight under the write-check policy, but content
    carries no owner of its own and no ordinary DELETE policy at all, so the content this owner's
    claims staked is read first, then removed separately on the owner-role admin connection,
    bypassing row level security entirely rather than reaching for `system_session()`'s own
    still-RLS-governed reach: content's DELETE policy already gates on `principal.is_admin` alone,
    but a bypassed connection needs no live principal row to carry that flag at all, the more
    robust seam for a teardown helper that must never leave a test's content orphaned behind.

    owner: principal whose rows to remove under the write-check policy.
    """
    async with acting_as(owner) as session:
        entity_content_ids = (
            (
                await session.execute(
                    text("SELECT content_id FROM entity_claim WHERE owner_id = :o"), {"o": owner}
                )
            )
            .scalars()
            .all()
        )
        # a raw text() statement carries no mapper, so it never picks up the do_orm_execute live
        # gate the way an ORM `select(FactClaim)` would; this plain WHERE already reads every
        # claim this owner ever wrote, live or superseded.
        fact_content_ids = (
            (
                await session.execute(
                    text("SELECT content_id FROM fact_claim WHERE owner_id = :o"), {"o": owner}
                )
            )
            .scalars()
            .all()
        )
        tables = (
            "fact_claim",
            "profile",
            "community",
            "entity_claim",
            "chunk",
            "document",
            "session_item",
            "watermark",
        )
        for table in tables:
            await session.execute(text(f"DELETE FROM {table} WHERE owner_id = :o"), {"o": owner})
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            if fact_content_ids:
                await connection.execute(
                    text("DELETE FROM fact_content WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": fact_content_ids},
                )
            if entity_content_ids:
                await connection.execute(
                    text("DELETE FROM entity_content WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": entity_content_ids},
                )
    finally:
        await admin.dispose()


@asynccontextmanager
async def owned_principal() -> AsyncIterator[uuid.UUID]:
    """Yield a fresh seeded principal and remove its rows and itself on exit.

    The teardown deletes the principal's owned rows as the owner so the write-check policy permits
    it, then the principal, so a failed assertion inside the body leaves nothing behind.
    """
    pid = uuid.uuid4()
    await add_principals(pid)
    try:
        yield pid
    finally:
        await purge_owner(pid)
        await drop_principals(pid)


# id-keyed wrappers over the Group/Principal model methods, one system-acting session per call,
# the shape the pre-refactor `aizk.auth` free functions carried; kept here since the DB test suite
# still reads and asserts against plain ids rather than the loaded objects the production MCP
# tool bodies now thread through a single shared session.


async def create_group(
    name: str, public: bool = False, curated: bool = False, creator: uuid.UUID | None = None
) -> uuid.UUID:
    """Create a group and return its id. See `Group.create`."""
    async with system_session() as session:
        group = await Group.create(session, name, public=public, curated=curated, creator=creator)
        return group.id


async def group_id_named(name: str) -> uuid.UUID:
    """Resolve a group name to its id. See `Group.named`."""
    async with system_session() as session:
        return (await Group.named(session, name)).id


async def add_member(principal_id: uuid.UUID, group_id: uuid.UUID, role: str = "writer") -> None:
    """Add a principal to a group by id. See `Group.add_member`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        await group.add_member(session, principal_id, role=role)


async def remove_member(principal_id: uuid.UUID, group_id: uuid.UUID) -> None:
    """Remove a principal from a group by id. See `Group.remove_member`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        await group.remove_member(session, principal_id)


async def group_admin(principal_id: uuid.UUID, group_id: uuid.UUID) -> bool:
    """Whether a principal administers a group by id. See `Group.admin`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        return await group.admin(session, principal_id)


async def require_group_admin(principal_id: uuid.UUID, group_id: uuid.UUID) -> None:
    """Refuse unless the principal administers the group. See `Group.require_admin`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        await group.require_admin(session, principal_id)


async def publish_group(group_id: uuid.UUID, public: bool = True) -> None:
    """Flip a group's public flag by id. See `Group.publish`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        await group.publish(session, public=public)


async def curate_group(group_id: uuid.UUID, curated: bool = True) -> None:
    """Flip a group's curation flag by id. See `Group.curate`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        await group.curate(session, curated=curated)


async def delete_group(group_id: uuid.UUID) -> None:
    """Delete a group by id, a harmless no-op when it is already gone. See `Group.delete`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        if group is not None:
            await group.delete(session)


async def list_groups() -> list[dict[str, str | bool | int]]:
    """List every group with its visibility and member count. See `Group.list_all`."""
    async with system_session() as session:
        return await Group.list_all(session)


async def pending_facts(group_id: uuid.UUID) -> list:
    """List a curated group's unreviewed claims by group id. See `Group.pending_facts`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        return await group.pending_facts(session)


async def approve_facts(group_id: uuid.UUID, fact_ids: list[uuid.UUID] | None) -> int:
    """Approve a curated group's pending claims by group id. See `Group.approve_facts`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        return await group.approve_facts(session, fact_ids)


async def reject_facts(group_id: uuid.UUID, fact_ids: list[uuid.UUID]) -> int:
    """Reject a curated group's pending claims by group id. See `Group.reject_facts`."""
    async with system_session() as session:
        group = await session.get(Group, group_id)
        assert group is not None
        return await group.reject_facts(session, fact_ids)


async def review_stamp(
    session: AsyncSession, scope: uuid.UUID | None, owner_id: uuid.UUID
) -> datetime | None:
    """The reviewed_at stamp a new claim should carry. See `Group.review_stamp`."""
    return await Group.review_stamp(session, scope, owner_id)


async def create_principal(display_name: str, kind: str | None = None) -> uuid.UUID:
    """Create a principal and return its id. See `Principal.create`.

    kind: accepted and ignored, the pre-refactor signature's now-deleted, inert discriminator.
    """
    async with system_session() as session:
        return (await Principal.create(session, display_name)).id


async def grant_admin(principal_id: uuid.UUID) -> None:
    """Mark a principal as an admin by id. See `Principal.grant_admin`."""
    async with system_session() as session:
        principal = await session.get(Principal, principal_id)
        assert principal is not None
        await principal.grant_admin(session)


async def is_admin(principal_id: uuid.UUID) -> bool:
    """Whether a principal administers the engine. See `Principal.administers`."""
    return await Principal.administers(principal_id)


async def list_principals() -> list[Principal]:
    """List every principal in first-seen order. See `Principal.list_all`."""
    async with system_session() as session:
        return await Principal.list_all(session)


async def recent_writes(principal_id: uuid.UUID, limit: int = 20) -> list:
    """List a principal's recent document writes. See `Principal.recent_writes`."""
    return await Principal.recent_writes(principal_id, limit=limit)
