import json
from pathlib import Path
from typing import TextIO

from loguru import logger
from patos import FrozenModel
from sqlmodel import select

from .config import settings
from .store import (
    Chunk,
    Document,
    Entity,
    Fact,
)
from .store.engine import Session
from .store.identity import User

type Exported = Document | Chunk | Entity.Content | Entity.Claim | Fact.Content | Fact.Claim


class ExportReport(FrozenModel):
    """The per-table row counts one scoped export wrote, the receipt an admin reads back."""

    documents: int
    chunks: int
    entity_content: int
    entity_claims: int
    fact_content: int
    fact_claims: int
    path: str

    def render(self) -> str:
        """One receipt line per exported table, ending with where the dump landed."""
        counts = self.model_dump(exclude={"path"})
        rows = "\n".join(f"{table}: {count}" for table, count in counts.items())
        return f"{rows}\nwritten to {self.path}"


async def _write_table(
    session: Session, output: TextIO, model: type[Exported], history: bool = False
) -> int:
    """Stream one visible table to `output` and return its row count."""
    statement = select(model).order_by(model.id)
    if history:
        statement = statement.execution_options(**{settings.skip_live_gate: True})
    rows = await session.stream_scalars(statement)
    count = 0
    async for row in rows:
        output.write(json.dumps(row.record(), ensure_ascii=False) + "\n")
        count += 1
    return count


async def export_scope(
    path: Path,
    user: User | None = None,
) -> ExportReport:
    """Dump the user-visible documents, chunks, entity/fact content, and claims to a JSONL."""
    user = user or User.system()
    with path.open("w", encoding="utf-8") as output:
        async with user as session:
            documents = await _write_table(session, output, Document)
            chunks = await _write_table(session, output, Chunk)
            entity_content = await _write_table(session, output, Entity.Content)
            entity_claims = await _write_table(session, output, Entity.Claim)
            fact_content = await _write_table(session, output, Fact.Content)
            fact_claims = await _write_table(session, output, Fact.Claim, history=True)
    logger.info(
        "exported {} documents, {} chunks, {} entity content, {} entity claims, "
        "{} fact content, {} fact claims to {} for user {}",
        documents,
        chunks,
        entity_content,
        entity_claims,
        fact_content,
        fact_claims,
        path,
        user.id,
    )
    return ExportReport(
        documents=documents,
        chunks=chunks,
        entity_content=entity_content,
        entity_claims=entity_claims,
        fact_content=fact_content,
        fact_claims=fact_claims,
        path=str(path),
    )
