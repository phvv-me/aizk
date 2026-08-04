from typing import cast

import dbutil
import pytest
from id_factory import uuid5
from pydantic import UUID5, UUID7
from sqlmodel import select

from aizk.config import settings
from aizk.graph.ids import entity_id, fact_id
from aizk.store import Fact
from aizk.store.engine import Session

pytestmark = pytest.mark.usefixtures("migrated_db")


async def seed_live_claim(owner: UUID5 | UUID7, statement: str, days_old: float) -> UUID5 | UUID7:
    subject = entity_id("subj", "concept")
    content = fact_id(subject, "related_to", None, statement)
    claim = uuid5()
    await dbutil.admin_exec(
        "INSERT INTO entity_content (id, name, type) VALUES (:i, 'subj', 'concept') "
        "ON CONFLICT (id) DO NOTHING",
        {"i": subject},
    )
    await dbutil.admin_exec(
        "INSERT INTO fact_content (id, subject_id, predicate, statement) "
        "VALUES (:i, :s, 'related_to', :st) ON CONFLICT (id) DO NOTHING",
        {"i": content, "s": subject, "st": statement},
    )
    await dbutil.admin_exec(
        "INSERT INTO fact_claim (id, content_id, created_by, scopes, recorded_from) "
        "VALUES (:i, :c, :o, CAST(:scopes AS uuid[]), "
        "now() - make_interval(days => :d))",
        {"i": claim, "c": content, "o": owner, "scopes": [str(owner)], "d": int(days_old)},
    )
    return claim


def test_forget_from_documents_retracts_with_the_forgotten_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_ids = [uuid5(), uuid5()]
    captured_ids: list[UUID5 | UUID7] = []
    captured_reasons: list[str] = []

    async def fake_retract(
        cls: type[Fact.Claim], session: Session, ids: list[UUID5 | UUID7], reason: str
    ) -> list[UUID5 | UUID7]:
        del cls, session
        captured_ids.extend(ids)
        captured_reasons.append(reason)
        return ids[:1]

    monkeypatch.setattr(Fact.Claim, "retract_from_documents", classmethod(fake_retract))

    retracted = dbutil.run(Fact.Claim.forget_from_documents(cast("Session", None), document_ids))

    assert retracted == document_ids[:1]
    assert captured_ids == document_ids
    assert captured_reasons == ["forgotten"]


def test_archive_stale_closes_forgotten_claims_only() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        await seed_live_claim(owner, "ancient", days_old=400)
        await seed_live_claim(owner, "recent", days_old=1)
        async with dbutil.actor(owner) as session:
            archived = await Fact.Claim.archive_stale(
                session, frozenset({owner}), half_life_days=90.0, floor=0.25
            )
        assert len(archived) == 1
        async with dbutil.actor(owner) as session:
            live = set(
                await session.exec(
                    select(Fact.Claim.id).execution_options(**{settings.skip_live_gate: True})
                )
            )
        assert archived[0] in live  # still present in history, just no longer open

    dbutil.run(body())
