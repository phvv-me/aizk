import uuid

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..exceptions import NotVisibleError
from ..store import EntityClaim, EntityContent, LiveFact, Membership, Profile, acting_as
from .models import ProfileReport
from .tier_builder import TierBuilder

Grounding = tuple[str, list[uuid.UUID], list[str]]


class ProfileTierBuilder(TierBuilder[Grounding, ProfileReport]):
    """One entity's structured-portrait pass, static identity plus its latest facts' dynamic state.

    subject_id: entity content this instance portrays.
    """

    def __init__(self, principal_id: uuid.UUID, subject_id: uuid.UUID) -> None:
        super().__init__(principal_id, settings.profile_system, ProfileReport)
        self.subject_id = subject_id
        self.profile_id: uuid.UUID | None = None

    async def subject_entity(self, session: AsyncSession) -> EntityContent:
        """This profile's subject content, raising when it is not visible to this principal."""
        entity = await session.get(EntityContent, self.subject_id)
        if entity is None:
            raise NotVisibleError(f"entity {self.subject_id} is not visible to build a profile")
        return entity

    async def representative_scopes(self, session: AsyncSession) -> list[uuid.UUID]:
        """A display-only scope set for this subject, never part of Profile's own uniqueness.

        This principal's own private claim on the content when one exists, else whichever of its
        own claims sorts first (the empty private array orders ahead of any non-empty set under
        Postgres's own array comparison, so `ORDER BY scopes` alone already prefers it). One entity
        content can carry several of this principal's claims across different scope sets while it
        still gets exactly one rolled-up profile, keyed only on (owner_id, subject_id).
        """
        scopes = await session.scalar(
            select(EntityClaim.scopes)
            .where(
                EntityClaim.content_id == self.subject_id,
                EntityClaim.owner_id == self.principal_id,
            )
            .order_by(EntityClaim.scopes)
            .limit(1)
        )
        return list(scopes or [])

    async def subject_statements(self, session: AsyncSession) -> list[str]:
        """This subject's visible latest fact statements, oldest first."""
        return list(
            await session.scalars(
                # `live_fact` already carries the current-and-reviewed gate
                select(LiveFact.statement)
                .where(
                    or_(
                        LiveFact.subject_id == self.subject_id,
                        LiveFact.object_id == self.subject_id,
                    )
                )
                .order_by(func.lower(LiveFact.recorded))
            )
        )

    async def gather(self) -> Grounding:
        """The subject's name, a representative scope set, and its latest facts."""
        async with acting_as(self.principal_id) as session:
            entity = await self.subject_entity(session)
            scopes = await self.representative_scopes(session)
            statements = await self.subject_statements(session)
            return entity.name, scopes, statements

    def body(self, grounding: Grounding) -> str:
        """Render the subject's name and latest facts as the structured call's user turn."""
        name, _scopes, statements = grounding
        facts = "Facts:\n" + "\n".join(f"- {statement}" for statement in statements)
        return f"Entity: {name}\n\n{facts}"

    def texts(self, report: ProfileReport) -> list[str]:
        """The one portrait paragraph this subject's report carries."""
        return [report.summary]

    async def upsert(
        self, grounding: Grounding, report: ProfileReport, vectors: list[list[float]]
    ) -> int:
        """Upsert the one profile row this subject holds, so a rebuild overwrites it in place.

        A single upsert on the owner-and-subject unique key, `store.models.Watermark.bump`'s own
        pattern, so a rebuild racing a concurrent one lands as one row rather than a duplicate
        insert or a lost update between a separate select and flush.
        """
        name, scopes, statements = grounding
        statement = (
            insert(Profile)
            .values(
                owner_id=self.principal_id,
                scopes=scopes,
                subject_id=self.subject_id,
                summary=report.summary,
                embedding=vectors[0],
            )
            .on_conflict_do_update(
                index_elements=["owner_id", "subject_id"],
                set_={"summary": report.summary, "embedding": vectors[0]},
            )
            .returning(Profile.id)
        )
        async with acting_as(self.principal_id) as session:
            self.profile_id = await session.scalar(statement)
        logger.info("built profile for entity {!r} from {} facts", name, len(statements))
        return 1


async def build_profile(
    subject_id: uuid.UUID,
    principal_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Summarize an entity's latest facts into a profile, embed it, upsert the row, return its id.

    Gathers the visible latest facts whose subject or object is the entity content, asks the LLM
    for a static-plus-dynamic paragraph grounded only in them, embeds the summary, and upserts the
    one profile row for that subject under the acting principal so a rebuild overwrites in place
    rather than piling up.

    subject_id: entity content the profile portrays.
    principal_id: identity that owns the profile and whose visibility scopes the facts read, the
        system principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    builder = ProfileTierBuilder(principal_id, subject_id)
    await builder.build()
    assert builder.profile_id is not None  # gather() raises rather than skipping when invisible
    return builder.profile_id


async def refresh_profiles(
    principal_id: uuid.UUID | None = None,
) -> int:
    """Rebuild the profile of every writable entity, the weekly full refresh, return how many.

    Lists the distinct entity content this principal holds a writable claim on, then rolls each
    one's latest facts into a fresh profile through `build_profile`, committing one entity at a
    time so a slow summarization never holds a write lock. This is the scheduled full pass the
    on-write debounced rebuilds complement, catching any entity whose facts changed without a
    write touching it directly. An entity claimed only in scopes the principal merely reads is
    left to that scope's own writers.

    principal_id: identity that owns the profiles and whose visibility scopes the entities, the
        system principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    async with acting_as(principal_id) as session:
        subject_ids = list(
            await session.scalars(
                select(EntityClaim.content_id)
                .where(
                    EntityClaim.owner_id == principal_id,
                    Membership.writable_scopes(EntityClaim.scopes, principal_id),
                )
                .distinct()
                .order_by(EntityClaim.content_id)
            )
        )
    for subject_id in subject_ids:
        await build_profile(subject_id, principal_id=principal_id)
    logger.info("refreshed {} profiles under principal {}", len(subject_ids), principal_id)
    return len(subject_ids)
