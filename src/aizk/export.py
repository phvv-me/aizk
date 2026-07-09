import json
import uuid
from pathlib import Path

from loguru import logger
from patos import FrozenModel
from sqlalchemy import select

from .config import settings
from .store import Chunk, Document, EntityClaim, EntityContent, FactClaim, FactContent, acting_as
from .store.engine import session


class ExportReport(FrozenModel):
    """The per-table row counts one scoped export wrote, the receipt an admin reads back.

    documents: source documents written.
    chunks: chunk spans written.
    entity_content: deduplicated entity content rows written.
    entity_claims: entity claim rows written.
    fact_content: deduplicated fact content rows written.
    fact_claims: fact claim rows written, including the superseded history rows.
    path: the JSONL file the dump landed in.
    """

    documents: int
    chunks: int
    entity_content: int
    entity_claims: int
    fact_content: int
    fact_claims: int
    path: str


async def export_scope(
    path: Path,
    user_id: uuid.UUID | None = None,
) -> ExportReport:
    """Dump the user-visible documents, chunks, entity/fact content, and claims to a JSONL.

    Runs entirely under `acting_as` so row level security decides exactly which rows leave, the
    user's own and its group-shared scopes and no other tenant's. Content's read-through-a-
    claim policy means only content this user's claims actually reach ever leaves too. The
    claim reads opt out of the live gate so superseded history and both temporal windows are
    dumped, not only the currently-valid edges. Emits only, no import path back in.

    path: the JSONL file the dump is written to.
    user_id: identity whose row level security visibility scopes exactly what is exported,
        the system user when null.
    """
    user_id = user_id or settings.system_user_id
    async with acting_as(user_id):
        documents = list(await session().scalars(select(Document).order_by(Document.id)))
        chunks = list(await session().scalars(select(Chunk).order_by(Chunk.id)))
        entity_content = list(
            await session().scalars(select(EntityContent).order_by(EntityContent.id))
        )
        entity_claims = list(await session().scalars(select(EntityClaim).order_by(EntityClaim.id)))
        fact_content = list(await session().scalars(select(FactContent).order_by(FactContent.id)))
        fact_claims = list(
            await session().scalars(
                select(FactClaim)
                .order_by(FactClaim.id)
                .execution_options(**{settings.skip_live_gate: True})
            )
        )
    # every row serializes through the one pydantic-driven TableBase.record, the fact claims
    # carrying both the valid and recorded ranges so the dump preserves the full bi-temporal
    # history
    records = [
        row.record()
        for row in [
            *documents,
            *chunks,
            *entity_content,
            *entity_claims,
            *fact_content,
            *fact_claims,
        ]
    ]
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    logger.info(
        "exported {} documents, {} chunks, {} entity content, {} entity claims, "
        "{} fact content, {} fact claims to {} for user {}",
        len(documents),
        len(chunks),
        len(entity_content),
        len(entity_claims),
        len(fact_content),
        len(fact_claims),
        path,
        user_id,
    )
    return ExportReport(
        documents=len(documents),
        chunks=len(chunks),
        entity_content=len(entity_content),
        entity_claims=len(entity_claims),
        fact_content=len(fact_content),
        fact_claims=len(fact_claims),
        path=str(path),
    )
