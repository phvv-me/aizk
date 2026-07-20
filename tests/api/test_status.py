import asyncio
from datetime import UTC, datetime, time

import pytest
from id_factory import uuid5

from aizk.config import settings
from aizk.status import (
    CallerStatus,
    ProcessingStatus,
    StatusReport,
    UsageReport,
    UsageStatus,
    UsageSummary,
)
from aizk.store.identity import OrganizationStanding, User


def status_user() -> User:
    """Build one caller with deliberately unsorted organization standing."""
    owner, private_lab, docs = uuid5(), uuid5(), uuid5()
    return User.authorized(
        owner,
        read=(owner, private_lab, docs),
        write=(owner, private_lab),
        name="Pedro Valois",
        username="pedro",
        avatar="https://example.com/avatar.png",
        roles=("aizk-user",),
        organizations=(
            OrganizationStanding(
                id=private_lab,
                name="Zeta Lab",
                roles=("editor",),
                permissions=("write:memory",),
            ),
            OrganizationStanding(
                id=docs,
                name="Docs",
                description="Public guidance",
                roles=("viewer",),
                permissions=("read:memory",),
                public=True,
            ),
        ),
    )


def test_caller_status_exposes_authority_without_internal_identifiers() -> None:
    status = CallerStatus.from_user(status_user())

    assert status.model_dump(mode="json") == {
        "name": "Pedro Valois",
        "username": "pedro",
        "avatar": "https://example.com/avatar.png",
        "label": "Pedro Valois",
        "roles": ["aizk-user"],
        "anonymous": False,
        "organizations": [
            {
                "name": "Docs",
                "description": "Public guidance",
                "roles": ["viewer"],
                "permissions": ["read:memory"],
                "writable": False,
                "public": True,
            },
            {
                "name": "Zeta Lab",
                "description": None,
                "roles": ["editor"],
                "permissions": ["write:memory"],
                "writable": True,
                "public": False,
            },
        ],
    }


def test_caller_status_marks_the_anonymous_identity() -> None:
    status = CallerStatus.from_user(User.private(settings.anonymous_user_id))

    assert status.anonymous is True
    assert status.label is None
    assert status.organizations == ()


def test_status_report_combines_usage_and_processing_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = status_user()
    now = datetime(2026, 7, 20, 1, tzinfo=UTC)
    start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    usage = UsageReport(
        generated_at=now,
        recorded_through=now,
        days=7,
        start=start,
        summary=UsageSummary(requests=3),
        lifetime=UsageSummary(requests=12),
    )
    processing = ProcessingStatus(generated_at=now, state="idle", stages=())
    calls: list[tuple[str, User, int | None]] = []
    gates = [asyncio.Event(), asyncio.Event()]

    async def load_usage(current: User, days: int) -> UsageReport:
        calls.append(("usage", current, days))
        gates[0].set()
        await gates[1].wait()
        return usage

    async def load_processing(current: User) -> ProcessingStatus:
        calls.append(("processing", current, None))
        gates[1].set()
        await gates[0].wait()
        return processing

    monkeypatch.setattr(UsageReport, "load", load_usage)
    monkeypatch.setattr(ProcessingStatus, "load", load_processing)

    before = datetime.now(UTC)
    report = asyncio.run(StatusReport.load(user, days=7))
    after = datetime.now(UTC)

    assert before <= report.generated_at <= after
    assert report.caller == CallerStatus.from_user(user)
    assert report.usage == UsageStatus.from_report(usage)
    assert "points" not in report.usage.model_dump()
    assert report.processing is processing
    assert calls == [
        ("usage", user, 7),
        ("processing", user, None),
    ]
