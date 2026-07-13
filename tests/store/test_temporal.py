import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import dbutil
import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.dialects.postgresql import Range
from sqlmodel import select

from aizk.config import settings
from aizk.graph.ids import entity_id, fact_id
from aizk.store import FactClaim

if TYPE_CHECKING:
    from aizk.store.engine import Session

pytestmark = pytest.mark.usefixtures("migrated_db")


def build_claim(recorded: Range, valid: Range | None, access_count: int = 0) -> FactClaim:
    return FactClaim(
        content_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        recorded=recorded,
        valid=valid,
        access_count=access_count,
    )


@given(access_count=st.integers(min_value=0, max_value=20))
def test_relevance_rises_with_access_and_decays_with_age(access_count: int) -> None:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    fresh = build_claim(Range(now, None), None, access_count=access_count)
    assert fresh.relevance(now, half_life_days=90.0) == pytest.approx(1.0 + access_count)
    old = build_claim(Range(now - timedelta(days=180), None), None, access_count=access_count)
    assert old.relevance(now, half_life_days=90.0) < fresh.relevance(now, half_life_days=90.0)


async def seed_live_claim(owner: uuid.UUID, statement: str, days_old: float) -> uuid.UUID:
    subject = entity_id("subj", "concept")
    content = fact_id("subj", "related_to", "", statement)
    claim = uuid.uuid4()
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
        "INSERT INTO fact_claim (id, content_id, created_by, scopes, recorded) "
        "VALUES (:i, :c, :o, CAST(:scopes AS uuid[]), "
        "tstzrange(now() - make_interval(days => :d), NULL))",
        {"i": claim, "c": content, "o": owner, "scopes": [str(owner)], "d": int(days_old)},
    )
    return claim


def test_forget_from_documents_retracts_with_the_forgotten_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_ids = [uuid.uuid4(), uuid.uuid4()]
    captured: dict[str, object] = {}

    async def fake_retract(
        cls: type, session: object, ids: list[uuid.UUID], reason: str
    ) -> list[uuid.UUID]:
        captured["ids"], captured["reason"] = ids, reason
        return ids[:1]

    monkeypatch.setattr(FactClaim, "retract_from_documents", classmethod(fake_retract))

    retracted = dbutil.run(FactClaim.forget_from_documents(cast("Session", None), document_ids))

    assert retracted == document_ids[:1]
    assert captured == {"ids": document_ids, "reason": "forgotten"}


def test_archive_stale_closes_forgotten_claims_only() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        await seed_live_claim(owner, "ancient", days_old=400)
        await seed_live_claim(owner, "recent", days_old=1)
        async with dbutil.actor(owner) as session:
            archived = await FactClaim.archive_stale(
                session, frozenset({owner}), half_life_days=90.0, floor=0.25
            )
        assert len(archived) == 1
        async with dbutil.actor(owner) as session:
            live = set(
                await session.exec(
                    select(FactClaim.id).execution_options(**{settings.skip_live_gate: True})
                )
            )
        assert archived[0] in live  # still present in history, just no longer open

    dbutil.run(body())
