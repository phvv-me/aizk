import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from graphdb import owned_principal
from sqlalchemy.dialects.postgresql import Range

from aizk.export import ExportReport, export_scope
from aizk.store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    acting_as,
)

# the fields a fact claim record must carry so both the valid-time and the transaction-time ranges
# of the bi-temporal history ride along in the dump
FACT_CLAIM_WINDOW_KEYS = {"valid", "recorded"}


async def seed(owner: uuid.UUID, tag: str) -> dict[str, uuid.UUID]:
    """Seed one owner with a document, chunk, entity content+claim, and a live and old fact claim.

    The old claim carries a closed `recorded` upper bound, so a correct export reads the whole
    bi-temporal history rather than only the currently-valid edge. Both fact claims share one
    content row, the deduplicated structure a content-addressed statement mints once.

    owner: principal the seeded rows belong to.
    tag: a distinctive marker woven into the ids-bearing text so a leak is unambiguous.
    """
    ids = {
        name: uuid.uuid4()
        for name in ("document", "chunk", "entity_content", "entity_claim", "fact_content", "old")
    }
    ids["live"] = uuid.uuid4()
    now = datetime.now(UTC)
    async with acting_as(owner) as session:
        session.add(
            Document(id=ids["document"], content_hash=uuid.uuid4().hex, owner_id=owner, title=tag)
        )
        session.add(
            Chunk(id=ids["chunk"], document_id=ids["document"], ord=0, text=tag, owner_id=owner)
        )
        session.add(EntityContent(id=ids["entity_content"], name=tag, type="Concept"))
        session.add(
            FactContent(
                id=ids["fact_content"],
                subject_id=ids["entity_content"],
                predicate="related_to",
                statement=f"{tag} old",
            )
        )
        # content and claim share no ORM relationship(), only a bare FK column, so content must
        # actually flush before a claim staking it is added, or the claim insert can race ahead
        # of its own content row and fail the foreign key.
        await session.flush()
        session.add(
            EntityClaim(id=ids["entity_claim"], content_id=ids["entity_content"], owner_id=owner)
        )
        session.add(
            FactClaim(
                id=ids["old"],
                content_id=ids["fact_content"],
                owner_id=owner,
                recorded=Range(now - timedelta(hours=1), now),
                valid=Range(None, now),
            )
        )
        session.add(
            FactClaim(
                id=ids["live"],
                content_id=ids["fact_content"],
                owner_id=owner,
            )
        )
    return ids


def all_ids(records: list[dict[str, object]]) -> set[str]:
    """Collect every id-shaped value across the export records, for the leak check.

    records: the parsed JSONL records the export wrote.
    """
    keys = ("id", "content_id", "document_id", "subject_id")
    return {str(record[key]) for record in records for key in keys if record.get(key)}


def test_export_emits_only_the_acting_principal_rows(requires_db: None, tmp_path: Path) -> None:
    """A scoped export dumps the admin's own rows and history and never another tenant's.

    Two principals each own a private slice, and exporting one under acting_as lets row level
    security decide exactly which rows leave, so the dump carries every id the acting principal
    owns, including the superseded claim, and none of the other's, the no-leak moat governing the
    export the same way it governs a recall.
    """

    async def run() -> tuple[ExportReport, list[dict[str, object]], set[str], set[str]]:
        async with owned_principal() as mine, owned_principal() as other:
            my_ids = await seed(mine, "mine")
            other_ids = await seed(other, "other")
            report = await export_scope(tmp_path / "dump.jsonl", principal_id=mine)
            records = [
                json.loads(line)
                for line in (tmp_path / "dump.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            mine_values = {str(value) for value in my_ids.values()}
            other_values = {str(value) for value in other_ids.values()}
            return report, records, mine_values, other_values

    report, records, mine_values, other_values = asyncio.run(run())

    dumped = all_ids(records)
    assert mine_values <= dumped  # every row the acting principal owns leaves
    assert not (other_values & dumped)  # no other tenant's row ever does
    assert report.documents == 1 and report.chunks == 1
    assert report.entity_content == 1 and report.entity_claims == 1
    assert report.fact_content == 1
    assert report.fact_claims == 2  # the live claim and the superseded history row both export
    fact_claims = [record for record in records if record["table"] == "fact_claim"]
    assert all(set(record) >= FACT_CLAIM_WINDOW_KEYS for record in fact_claims)
