import uuid
from collections import defaultdict

from loguru import logger
from patos import FrozenModel
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select

from ..config import settings
from ..exceptions import NotVisibleError
from ..extract.llm import structured
from ..serving import embed
from ..store import EntityClaim, EntityContent, LiveFact, Profile
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes
from .models import ProfileReport


class ProfileGrounding(FrozenModel):
    """One entity and the ordered current facts that ground its profile."""

    subject_id: uuid.UUID
    name: str
    statements: tuple[str, ...]


class ProfileDraft(FrozenModel):
    """A summarized profile ready for one bulk upsert."""

    subject_id: uuid.UUID
    summary: str
    vector: tuple[float, ...]


class ProfileBuilder:
    """Load, summarize, embed, and store profiles in bounded database phases."""

    __slots__ = ("scopes",)

    def __init__(self, scopes: Scopes) -> None:
        self.scopes = frozenset(scopes)

    async def snapshot(
        self, session: Session, subject_ids: list[uuid.UUID] | None = None
    ) -> list[ProfileGrounding]:
        """Load entity names and all related current fact statements in two queries."""
        roster = select(EntityContent.id, EntityContent.name).where(
            EntityContent.id.in_(select(EntityClaim.content_id))
        )
        if subject_ids is not None:
            roster = roster.where(EntityContent.id.in_(subject_ids))
        entities = dict((await session.exec(roster.order_by(EntityContent.id))).all())
        if subject_ids is not None:
            missing = set(subject_ids) - entities.keys()
            if missing:
                raise NotVisibleError(
                    f"entities {sorted(missing)} are not visible to build profiles"
                )
        statements: dict[uuid.UUID, list[str]] = defaultdict(list)
        if entities:
            rows = await session.exec(LiveFact.touching(entities))
            for subject_id, object_id, statement in rows:
                statements[subject_id].append(statement)
                if object_id in entities and object_id != subject_id:
                    statements[object_id].append(statement)
        return [
            ProfileGrounding(
                subject_id=subject_id,
                name=name,
                statements=tuple(statements[subject_id]),
            )
            for subject_id, name in entities.items()
        ]

    async def summarize(self, groundings: list[ProfileGrounding]) -> list[ProfileDraft]:
        """Summarize every grounding and embed all resulting profiles in one batch."""
        reports = [
            await structured(
                settings.profile_system,
                f"Entity: {grounding.name}\n\nFacts:\n"
                + "\n".join(f"- {statement}" for statement in grounding.statements),
                ProfileReport,
            )
            for grounding in groundings
        ]
        vectors = (
            await embed([report.summary for report in reports], mode="document") if reports else []
        )
        return [
            ProfileDraft(
                subject_id=grounding.subject_id,
                summary=report.summary,
                vector=tuple(vector),
            )
            for grounding, report, vector in zip(groundings, reports, vectors, strict=True)
        ]

    async def store(
        self, session: Session, drafts: list[ProfileDraft]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Upsert a complete profile batch and return IDs keyed by subject."""
        if not drafts:
            return {}
        statement = insert(Profile).values(
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
        statement = statement.on_conflict_do_update(
            index_elements=["scopes", "subject_id"],
            set_={
                "summary": statement.excluded.summary,
                "embedding": statement.excluded.embedding,
            },
        ).returning(Profile.subject_id, Profile.id)
        return dict((await session.exec(statement)).all())


async def build_profile(
    subject_id: uuid.UUID,
    scopes: Scopes | None = None,
) -> uuid.UUID:
    """Rebuild one entity profile through short read and write transactions."""
    key = frozenset(scopes or (settings.system_user_id,))
    builder = ProfileBuilder(key)
    async with User.system(key) as session:
        groundings = await builder.snapshot(session, [subject_id])
    drafts = await builder.summarize(groundings)
    async with User.system(key) as session:
        profile_ids = await builder.store(session, drafts)
    logger.info(
        "built profile for entity {} from {} facts", subject_id, len(groundings[0].statements)
    )
    return profile_ids[subject_id]


async def refresh_profiles(
    scopes: Scopes | None = None,
) -> int:
    """Rebuild every visible profile with one snapshot and one bulk write."""
    key = frozenset(scopes or (settings.system_user_id,))
    builder = ProfileBuilder(key)
    async with User.system(key) as session:
        groundings = await builder.snapshot(session)
    drafts = await builder.summarize(groundings)
    async with User.system(key) as session:
        await builder.store(session, drafts)
    logger.info("refreshed {} profiles in scope {}", len(drafts), key)
    return len(drafts)
