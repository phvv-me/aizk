import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import dbutil
from sqlalchemy.dialects.postgresql import Range

from aizk.config import settings
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

# every table an export line can be tagged with, the tag `TableBase.record` writes.
EXPORTED_TABLES = {
    "document",
    "chunk",
    "entity_content",
    "entity_claim",
    "fact_content",
    "fact_claim",
}

# a fact claim record must carry both the valid-time and the transaction-time ranges so the whole
# bi-temporal window rides along in the dump, not only the currently-valid edge.
FACT_CLAIM_WINDOW_KEYS = {"valid", "recorded"}


async def seed_graph(owner: uuid.UUID, tag: str) -> dict[str, uuid.UUID]:
    """Seed one owner with a document, chunk, entity content+claim, and a live and superseded fact.

    The superseded claim carries a closed `recorded` upper bound, so a correct export that opts out
    of the live gate reads the whole bi-temporal history, not only the currently-valid edge. Both
    fact claims stake one content row, the deduplicated structure a content-addressed statement
    mints once.

    owner: principal the seeded rows belong to.
    tag: distinctive marker woven into the row text so a cross-tenant leak is unambiguous.
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
                statement=f"{tag} statement",
            )
        )
        # content and claim share only a bare FK column, no ORM relationship, so content must flush
        # before a claim staking it is added or the claim insert races ahead of its content row.
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
        session.add(FactClaim(id=ids["live"], content_id=ids["fact_content"], owner_id=owner))
    return ids


def id_values(records: list[dict[str, object]]) -> set[str]:
    """Every id-shaped value across the export records, the surface a cross-tenant leak shows on.

    records: the parsed JSONL records the export wrote.
    """
    keys = ("id", "content_id", "document_id", "subject_id")
    return {str(record[key]) for record in records for key in keys if record.get(key)}


def read_jsonl(path: Path) -> list[dict[str, object]]:
    """Parse the JSONL dump into one record per non-empty line."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_export_dumps_the_acting_slice_with_history_and_no_other_tenant(
    migrated_db: None, tmp_path: Path
) -> None:
    """A scoped export dumps the principal's own rows and fact history, never another tenant's.

    Two principals each own a private slice; exporting one under `acting_as` lets row level
    security decide exactly which rows leave, so the dump carries every id the acting principal
    owns, including the superseded claim the live-gate opt-out preserves, and none of the other's.
    The report counts match the rows written and every line is tagged with a known table.
    """

    async def run() -> tuple[ExportReport, list[dict[str, object]], set[str], set[str]]:
        await dbutil.reset_db()
        mine = await dbutil.seed_user(uuid.uuid4())
        other = await dbutil.seed_user(uuid.uuid4())
        my_ids = await seed_graph(mine, "mine")
        other_ids = await seed_graph(other, "other")
        report = await export_scope(tmp_path / "dump.jsonl", principal_id=mine)
        records = read_jsonl(tmp_path / "dump.jsonl")
        mine_values = {str(value) for value in my_ids.values()}
        other_values = {str(value) for value in other_ids.values()}
        return report, records, mine_values, other_values

    report, records, mine_values, other_values = dbutil.run(run())

    dumped = id_values(records)
    assert mine_values <= dumped  # every row the acting principal owns leaves
    assert not (other_values & dumped)  # no other tenant's row ever does
    assert {record["table"] for record in records} <= EXPORTED_TABLES
    assert (report.documents, report.chunks) == (1, 1)
    assert (report.entity_content, report.entity_claims) == (1, 1)
    assert report.fact_content == 1
    assert report.fact_claims == 2  # the live claim and the superseded history row both export
    assert report.path == str(tmp_path / "dump.jsonl")
    # the per-table report counts are exactly the number of lines each table contributes.
    counts = {table: 0 for table in EXPORTED_TABLES}
    for record in records:
        counts[str(record["table"])] += 1
    assert counts == {
        "document": 1,
        "chunk": 1,
        "entity_content": 1,
        "entity_claim": 1,
        "fact_content": 1,
        "fact_claim": 2,
    }
    fact_claims = [record for record in records if record["table"] == "fact_claim"]
    assert all(set(record) >= FACT_CLAIM_WINDOW_KEYS for record in fact_claims)


def test_export_defaults_the_principal_to_the_system_identity(
    migrated_db: None, tmp_path: Path
) -> None:
    """With no principal given the export scopes to the system principal, dumping its own slice."""

    async def run() -> tuple[ExportReport, list[dict[str, object]], set[str]]:
        await dbutil.reset_db()
        await dbutil.seed_user(settings.system_user_id)
        ids = await seed_graph(settings.system_user_id, "system")
        report = await export_scope(tmp_path / "system.jsonl")
        records = read_jsonl(tmp_path / "system.jsonl")
        return report, records, {str(value) for value in ids.values()}

    report, records, seeded = dbutil.run(run())

    assert seeded <= id_values(records)
    assert report.fact_claims == 2  # the default-principal path still opts out of the live gate
