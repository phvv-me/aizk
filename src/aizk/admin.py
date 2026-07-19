from pathlib import Path
from typing import cast

from patos import FrozenModel
from pydantic import UUID5, UUID7, TypeAdapter
from sqlalchemy import func, literal, union_all
from sqlmodel import select
from sqlmodel.sql.expression import Select

from . import export, graph, ops
from .background.jobs.projection import enqueue_pending
from .background.status import TasksStatus, tasks_overview
from .config import settings
from .extract import ingest as extract_ingest
from .extract.extractor import Extractor
from .extract.models import Extraction
from .graph.build import GraphClients
from .graph.grounding import FactGrounding, GroundedProjection
from .ontology import Ontology
from .provenance import CaptureContext
from .serving.embed import EmbedClient, Embedder
from .serving.extract import LLM
from .store import (
    Chunk,
    Document,
    Entity,
    Fact,
    Relation,
)
from .store.identity import User
from .store.models.tables import RelationPolicy


class ForgetResult(FrozenModel):
    """What one forget retracted, the erasure counterpart to a write."""

    documents: list[str]
    claims: int


class OntologyKindRow(FrozenModel):
    """One ontology kind with how much of the graph uses it, the catalog inspection row."""

    name: str
    kind: str
    description: str
    domain: str
    structural: bool
    uses: int


class ExtractionDiagnostic(FrozenModel):
    """One stored chunk and its read-only model grounding result."""

    chunk_id: UUID7
    document_id: UUID7
    document_title: str | None
    source_chars: int
    proposed: Extraction
    grounding: tuple[FactGrounding, ...]
    accepted: GroundedProjection


def system() -> UUID5:
    """The system user id, the identity an operator's CLI call acts as by default."""
    return settings.system_user_id


async def rebuild(
    clients: GraphClients,
    limit: int | None = None,
    source: str | None = None,
    user_id: UUID5 | None = None,
) -> tuple[int, int]:
    """Build the graph now over the user's pending chunks, the on-demand extraction."""
    return await graph.build_graph(
        clients, limit=limit, scopes=frozenset({user_id or system()}), source=source
    )


async def decay(half_life_days: float = 90.0, user_id: UUID5 | None = None) -> int:
    """Run the decay pass now, archiving stale facts that leave recall but stay in history."""
    return await graph.decay(
        scopes=frozenset({user_id or system()}), half_life_days=half_life_days
    )


async def reembed(user_id: UUID5 | None = None) -> int:
    """Re-embed every visible stored vector with the current embedder, a backend migration."""
    return await graph.reembed(scopes=frozenset({user_id or system()}))


async def communities(user_id: UUID5 | None = None) -> int:
    """Build graph communities and their global summaries now."""
    return await graph.build_communities(scopes=frozenset({user_id or system()}))


async def raptor(llm: LLM, embed: Embedder, user_id: UUID5 | None = None) -> int:
    """Build the RAPTOR tree now, the recursive summary tiers above the communities."""
    return await graph.build_raptor(llm, embed, scopes=frozenset({user_id or system()}))


async def forget(query: str, k: int = 8, user_id: UUID5 | None = None) -> ForgetResult:
    """Retract the claims a query's own source notes contributed, remember's erasure
    counterpart."""
    actor = user_id or system()
    [vector] = await EmbedClient.from_settings(settings).embed([query], mode="query")
    async with User.system({actor}) as session:
        distance = Chunk.embedding @ vector
        doc_ids = list(
            await session.exec(
                select(Chunk.document_id)
                .where(Chunk.embedding.is_not(None))
                .group_by(Chunk.document_id)
                .order_by(func.min(distance))
                .limit(k)
            )
        )
        titles = list(await session.exec(select(Document.title).where(Document.id.in_(doc_ids))))
        retracted = await Fact.Claim.forget_from_documents(session, doc_ids)
    return ForgetResult(documents=[t for t in titles if t], claims=len(retracted))


async def promote(document: str, to_scopes: str, user_id: UUID5 | None = None) -> int:
    """Promote a document and its chunks and facts into a wider scope-set as a new audited
    copy."""
    actor = user_id or system()
    target = settings.scope_ids(to_scopes)
    authority = frozenset((actor, *target))
    user = User.authorized(actor, read=authority, write=authority)
    return await graph.promote([TypeAdapter(UUID7).validate_python(document)], target, user)


async def ingest(path: str, scopes: str | None = None, user_id: UUID5 | None = None) -> int:
    """Ingest a file or directory of notes and code into memory, the document count back."""
    actor = user_id or system()
    target = settings.scope_ids(scopes) or frozenset({actor})
    ingested = await extract_ingest.ingest_path(
        User.system(target), Path(path), created_by=actor, scopes=target
    )
    await enqueue_pending(scopes=target)
    return ingested


async def export_scope(path: str, user_id: UUID5 | None = None) -> export.ExportReport:
    """Export a user's visible memory to a JSONL file, the scoped portable dump."""
    return await export.export_scope(Path(path), user=User.system({user_id or system()}))


async def audit(limit: int = 20, user_id: UUID5 | None = None) -> list[Document]:
    """The most recent visible document writes, for the operator's write log."""
    actor = user_id or system()
    statement = select(Document).order_by(Document.created_at.desc()).limit(limit)
    return list(await User.system({actor}).exec[Document](statement))


async def diagnose_extraction(extractor: Extractor, chunk_id: UUID7) -> ExtractionDiagnostic:
    """Run extraction and grounding on one stored chunk without changing its graph state."""
    user = User.system()
    async with user.owner as session:
        chunk = await session.get(Chunk, chunk_id)
        if chunk is None:
            raise ValueError(f"unknown chunk {chunk_id}")
        document = await session.get(Document, chunk.document_id)
        await Ontology.ensure(session)
    capture = CaptureContext.model_validate(chunk.provenance)
    extraction = await extractor.extract(capture.search_text(chunk.text))
    accepted = GroundedProjection.from_extraction(extraction, chunk.text)
    return ExtractionDiagnostic(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        document_title=document.title if document is not None else None,
        source_chars=len(chunk.text),
        proposed=extraction,
        grounding=GroundedProjection.audit(extraction, chunk.text),
        accepted=accepted,
    )


async def define_entity_kind(name: str, description: str, domain: str = "general") -> None:
    """Add or refine an entity type in the live ontology and refresh its prompt."""
    async with User.system() as session:
        await Ontology.define_entity(session, name, description, domain)


async def define_relation_kind(
    name: str,
    description: str,
    domain: str = "general",
    policy: RelationPolicy = Relation.Policy.set,
) -> None:
    """Add or refine a relation predicate in the live ontology and refresh its prompt."""
    async with User.system() as session:
        await Ontology.define_relation(session, name, description, domain, policy)


async def list_ontology() -> list[OntologyKindRow]:
    """Every ontology kind with how much of the graph uses it, the catalog inspection surface."""
    entity_uses = (
        select(Entity.Content.id.count())
        .where(Entity.Content.type == Entity.Kind.name)
        .correlate(Entity.Kind)
        .scalar_subquery()
    )
    relation_uses = (
        select(Fact.Content.id.count())
        .where(Fact.Content.predicate == Relation.Kind.name)
        .correlate(Relation.Kind)
        .scalar_subquery()
    )
    statement = union_all(
        select(
            Entity.Kind.name,
            literal("entity").label("kind"),
            Entity.Kind.description,
            Entity.Kind.domain,
        ).add_columns(Entity.Kind.structural, entity_uses.label("uses")),
        select(
            Relation.Kind.name,
            literal("relation").label("kind"),
            Relation.Kind.description,
            Relation.Kind.domain,
        ).add_columns(Relation.Kind.structural, relation_uses.label("uses")),
    ).order_by("kind", "name")
    async with User.system() as session:
        # `union_all` yields a `CompoundSelect`, which sqlmodel's `exec` runs (returning the
        # same tuple rows a `Select` would) but does not cover in its overloads, so bridge it.
        rows = (
            await session.exec(cast("Select[tuple[str, str, str, str, bool, int]]", statement))
        ).all()
    return [OntologyKindRow.model_validate(row, from_attributes=True) for row in rows]


async def tasks_status() -> TasksStatus:
    """The autonomous engine's pending, running, failed, last-run, and lag counts."""
    return await tasks_overview()


async def reset_database() -> ops.ResetReport:
    """Recreate only Aizk's database and reinstall the ready schema."""
    return await ops.reset()


async def setup() -> ops.SetupReport:
    """Bring the database to a ready state, migrating to head and installing the queue
    schema."""
    return await ops.setup()


async def health() -> ops.HealthReport:
    """The engine's schema, row security, row-count, queue, and serving-endpoint state."""
    return await ops.health()
