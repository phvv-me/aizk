import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import dbutil
from id_factory import uuid5, uuid8
from pydantic import UUID5, UUID7, JsonValue
from sqlalchemy.dialects.postgresql import Range

from aizk.config import settings
from aizk.export import ExportReport, export_scope
from aizk.store import (
    Chunk,
    Document,
    Entity,
    Fact,
)
from aizk.store.identity import User

# Tables represented in an export stream
EXPORTED_TABLES = {
    "document",
    "chunk",
    "entity_content",
    "entity_claim",
    "fact_content",
    "fact_claim",
}

FACT_CLAIM_WINDOW_KEYS = {"valid", "recorded"}

type JSONRecord = dict[str, JsonValue]


async def seed_graph(owner: UUID5 | UUID7, tag: str) -> dict[str, UUID5 | UUID7]:
    ids = {
        name: uuid5()
        for name in ("document", "chunk", "entity_content", "entity_claim", "fact_content", "old")
    }
    ids["live"] = uuid5()
    now = datetime.now(UTC)
    async with dbutil.actor(owner) as session:
        session.add(
            Document(
                id=ids["document"],
                content_hash=uuid8(),
                created_by=owner,
                scopes=[owner],
                title=tag,
            )
        )
        session.add(
            Chunk(
                id=ids["chunk"],
                document_id=ids["document"],
                ord=0,
                text=tag,
                created_by=owner,
                scopes=[owner],
            )
        )
        session.add(Entity.Content(id=ids["entity_content"], name=tag, type="concept"))
        session.add(
            Fact.Content(
                id=ids["fact_content"],
                subject_id=ids["entity_content"],
                predicate="related_to",
                statement=f"{tag} statement",
            )
        )
        # Flush content before its claim because this edge has no ORM relationship ordering.
        await session.flush()
        session.add(
            Entity.Claim(
                id=ids["entity_claim"],
                content_id=ids["entity_content"],
                created_by=owner,
                scopes=[owner],
            )
        )
        session.add(
            Fact.Claim(
                id=ids["old"],
                content_id=ids["fact_content"],
                created_by=owner,
                scopes=[owner],
                recorded=Range(now - timedelta(hours=1), now),
                valid=Range(None, now),
            )
        )
        session.add(
            Fact.Claim(
                id=ids["live"], content_id=ids["fact_content"], created_by=owner, scopes=[owner]
            )
        )
    return ids


def id_values(records: list[JSONRecord]) -> set[str]:
    keys = ("id", "content_id", "document_id", "subject_id")
    return {str(record[key]) for record in records for key in keys if record.get(key)}


def read_jsonl(path: Path) -> list[JSONRecord]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_export_streams_the_acting_slice_with_history_and_defaults_to_system(
    migrated_db: None, tmp_path: Path
) -> None:
    async def run() -> tuple[
        ExportReport,
        list[JSONRecord],
        set[str],
        set[str],
        ExportReport,
        list[JSONRecord],
        set[str],
    ]:
        await dbutil.reset_db()
        mine = uuid5()
        other = uuid5()
        my_ids = await seed_graph(mine, "mine")
        other_ids = await seed_graph(other, "other")
        system_ids = await seed_graph(settings.system_user_id, "system")
        report = await export_scope(tmp_path / "dump.jsonl", user=User.private(mine))
        records = read_jsonl(tmp_path / "dump.jsonl")
        system_report = await export_scope(tmp_path / "system.jsonl")
        system_records = read_jsonl(tmp_path / "system.jsonl")
        mine_values = {str(value) for value in my_ids.values()}
        other_values = {str(value) for value in other_ids.values()}
        return (
            report,
            records,
            mine_values,
            other_values,
            system_report,
            system_records,
            {str(value) for value in system_ids.values()},
        )

    report, records, mine_values, other_values, system_report, system_records, system_ids = (
        dbutil.run(run())
    )

    dumped = id_values(records)
    assert mine_values <= dumped  # every row the acting user owns leaves
    assert not (other_values & dumped)  # no other tenant's row ever does
    assert {record["table"] for record in records} <= EXPORTED_TABLES
    assert (report.documents, report.chunks) == (1, 1)
    assert (report.entity_content, report.entity_claims) == (1, 1)
    assert report.fact_content == 1
    assert report.fact_claims == 2  # the live claim and the superseded history row both export
    assert report.path == str(tmp_path / "dump.jsonl")
    assert "documents: 1" in report.render()
    assert report.render().endswith(f"written to {report.path}")
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
    assert system_ids <= id_values(system_records)
    assert system_report.fact_claims == 2
