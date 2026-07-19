import asyncio
from collections import defaultdict
from collections.abc import Sequence
from itertools import batched
from typing import cast

from loguru import logger
from patos import FrozenFlexModel, FrozenModel
from pydantic import UUID5, UUID7, TypeAdapter
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select

from ..config import settings
from ..exceptions import NotVisibleError
from ..serving.embed import Embedder
from ..serving.extract import LLM
from ..store import Entity, Fact, Profile, Watermark
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes
from .models import ProfileReport

_uuid5_adapter = TypeAdapter(UUID5)


class ProfileGrounding(FrozenModel):
    """One entity and the ordered current facts that ground its profile."""

    subject_id: UUID5
    name: str
    statements: tuple[str, ...]


class ProfileDraft(FrozenModel):
    """A summarized profile ready for one bulk upsert."""

    subject_id: UUID5
    summary: str
    vector: tuple[float, ...]


class ProfileBuilder(FrozenFlexModel):
    """Load, summarize, embed, and store profiles in bounded database phases."""

    scopes: Scopes
    llm: LLM
    embed: Embedder

    async def snapshot(
        self, session: Session, subject_ids: list[UUID5] | None = None
    ) -> list[ProfileGrounding]:
        """Load entity names and all related current fact statements in two queries."""
        roster = select(Entity.Content.id, Entity.Content.name).where(
            Entity.Content.id.in_(select(Entity.Claim.content_id))
        )
        if subject_ids is not None:
            roster = roster.where(Entity.Content.id.in_(subject_ids))
        entities = dict((await session.exec(roster.order_by(Entity.Content.id))).all())
        statements: dict[UUID5, list[str]] = defaultdict(list)
        if entities:
            rows = await session.exec(Fact.Live.touching(entities, settings.profile_facts_k))
            for entity_id, statement in rows:
                statements[entity_id].append(statement)
        return [
            ProfileGrounding(
                subject_id=subject_id,
                name=name,
                statements=tuple(statements[subject_id]),
            )
            for subject_id, name in entities.items()
            if statements[subject_id]
        ]

    async def summarize(self, groundings: list[ProfileGrounding]) -> list[ProfileDraft]:
        """Summarize every grounding and embed all resulting profiles in one batch."""
        reports: list[ProfileReport] = []
        for group in batched(groundings, settings.profile_build_concurrency, strict=False):
            reports.extend(
                await asyncio.gather(
                    *(
                        self.llm.generate(
                            settings.profile_system,
                            f"Entity: {grounding.name}\n\nFacts:\n"
                            + "\n".join(f"- {statement}" for statement in grounding.statements),
                            ProfileReport,
                        )
                        for grounding in group
                    )
                )
            )
        vectors = (
            await self.embed.embed([report.summary for report in reports], mode="document")
            if reports
            else []
        )
        return [
            ProfileDraft(
                subject_id=grounding.subject_id,
                summary=report.summary,
                vector=tuple(vector),
            )
            for grounding, report, vector in zip(groundings, reports, vectors, strict=True)
        ]

    async def store(self, session: Session, drafts: list[ProfileDraft]) -> dict[UUID5, UUID7]:
        """Upsert a complete profile batch and return IDs keyed by subject."""
        if not drafts:
            return {}
        base_statement = insert(Profile).values(
            [
                {
                    "created_by": settings.system_user_id,
                    "scopes": sorted(self.scopes),
                    "subject_id": draft.subject_id,
                    "summary": draft.summary,
                    "embedding": list(draft.vector),
                }
                for draft in drafts
            ]
        )
        statement = base_statement.on_conflict_do_update(
            index_elements=["scopes", "subject_id"],
            set_={
                "summary": base_statement.excluded.summary,
                "embedding": base_statement.excluded.embedding,
            },
        ).returning(Profile.subject_id, Profile.id)
        rows = cast("Sequence[tuple[UUID5, UUID7]]", (await session.exec(statement)).all())
        return dict(rows)


async def build_profile(
    subject_id: UUID5,
    llm: LLM,
    embed: Embedder,
    scopes: Scopes | None = None,
) -> UUID7:
    """Rebuild one entity profile through short read and write transactions."""
    key = frozenset(scopes or (settings.system_user_id,))
    builder = ProfileBuilder(scopes=key, llm=llm, embed=embed)
    async with User.system(key) as session:
        groundings = await builder.snapshot(session, [subject_id])
    if not groundings:
        raise NotVisibleError(f"entity {subject_id} is not visible or has no facts")
    drafts = await builder.summarize(groundings)
    async with User.system(key) as session:
        profile_ids = await builder.store(session, drafts)
    logger.info(
        "built profile for entity {} from {} facts", subject_id, len(groundings[0].statements)
    )
    return profile_ids[subject_id]


async def refresh_profiles(
    llm: LLM,
    embed: Embedder,
    scopes: Scopes | None = None,
) -> int:
    """Rebuild every visible profile with one snapshot and one bulk write."""
    key = frozenset(scopes or (settings.system_user_id,))
    builder = ProfileBuilder(scopes=key, llm=llm, embed=embed)
    async with User.system(key) as session:
        groundings = await builder.snapshot(session)
    drafts = await builder.summarize(groundings)
    async with User.system(key) as session:
        await builder.store(session, drafts)
    logger.info("refreshed {} profiles in scope {}", len(drafts), key)
    return len(drafts)


async def refresh_dirty_profiles(
    llm: LLM,
    embed: Embedder,
    scopes: Scopes | None = None,
) -> int:
    """Rebuild one bounded dirty-entity batch and consume only its observed increments."""
    key = frozenset(scopes or (settings.system_user_id,))
    builder = ProfileBuilder(scopes=key, llm=llm, embed=embed)
    async with User.system(key) as session:
        counters = await Watermark.pending_refs(
            session,
            key,
            Watermark.Kind.entity_dirty,
            settings.profile_batch_size,
        )
        groundings = await builder.snapshot(
            session,
            [_uuid5_adapter.validate_python(ref) for ref in counters],
        )
    drafts = await builder.summarize(groundings)
    async with User.system(key) as session:
        await builder.store(session, drafts)
        await Watermark.consume(
            session,
            key,
            Watermark.Kind.entity_dirty,
            counters,
        )
    logger.info("refreshed {} dirty profiles in scope {}", len(drafts), key)
    return len(drafts)
