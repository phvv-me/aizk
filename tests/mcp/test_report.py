from typing import get_type_hints
from unittest.mock import AsyncMock

import dbutil
import mcp.types as mt
import mcp_probe
import pytest
from doubles import RecordingEmbedder
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.tools import FunctionTool
from id_factory import uuid5, uuid7
from mcp_probe import build_server, context_for, tools_of
from pydantic import UUID5, UUID7, TypeAdapter, ValidationError
from sqlalchemy import text as sql

import aizk.memory as memory_module
from aizk.config import settings
from aizk.retrieval import RecallEvidence
from aizk.retrieval.models import Candidate, Lane
from aizk.memory import WriteResult
from aizk.provenance import CaptureContext
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


def _reporter(user_id: UUID5 | None = None) -> User:
    """A caller carrying exactly the write authority `LogtoClient` grants every account."""
    identity = user_id or uuid5()
    return User.authorized(identity, read=(identity,), write=(identity, settings.reports_scope_id))


def _operator(user_id: UUID5 | None = None) -> User:
    """A caller carrying the tenant admin role's read authority over filed reports."""
    identity = user_id or uuid5()
    return User.authorized(
        identity,
        read=(identity, settings.reports_scope_id),
        write=(identity, settings.reports_scope_id),
    )


def test_report_writes_a_text_document_into_the_fixed_operator_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reporter = _reporter()
    document_id = uuid7()
    writes: list[
        tuple[User, str, str | None, str | None, UUID5, frozenset[UUID5], CaptureContext]
    ] = []
    queued: list[tuple[UUID7, frozenset[UUID5]]] = []
    wakes: list[None] = []

    class Wake:
        async def wake(self) -> None:
            wakes.append(None)

    tools = tools_of(build_server(wake=Wake()))

    async def stub(
        user: User,
        text: str,
        title: str | None = None,
        source_uri: str | None = None,
        created_by: UUID5 | None = None,
        scopes: frozenset[UUID5] = frozenset(),
        capture: CaptureContext | None = None,
    ) -> UUID7:
        assert created_by is not None and capture is not None
        writes.append((user, text, title, source_uri, created_by, scopes, capture))
        return document_id

    async def queue(identifier: UUID7, scopes: frozenset[UUID5]) -> int:
        queued.append((identifier, scopes))
        return 1

    monkeypatch.setattr(memory_module.extract_ingest, "ingest_text", stub)
    monkeypatch.setattr(memory_module, "enqueue_document", queue)

    result = dbutil.run(
        tools["report"].fn(
            text="find returned nothing for a question the corpus plainly answers.",
            context=context_for(reporter, mt.Implementation(name="claude-code", version="2.1.0")),
        )
    )

    target = frozenset({settings.reports_scope_id})
    assert result == WriteResult(id=document_id)
    assert writes == [
        (
            # The write runs as a system transaction scoped to exactly the report scope,
            # never as the reporter, since the reporter can never read that scope back.
            User.system(target),
            "find returned nothing for a question the corpus plainly answers.",
            None,
            None,
            reporter.id,
            target,
            CaptureContext(speaker_label=reporter.label, client="claude-code/2.1.0"),
        )
    ]
    assert queued == [(document_id, target)]
    assert wakes == [None]


def test_report_is_invisible_to_the_reporter_and_visible_only_to_an_operator(
    fake_embedder: RecordingEmbedder,
    tools: dict[str, FunctionTool],
) -> None:
    reporter = _reporter()

    result = dbutil.run(
        tools["report"].fn(
            text="Two settled facts about the same launch date contradict each other.",
            context=context_for(reporter),
        )
    )

    # The real RLS path, not a Python condition: the writer cannot select its own report back.
    assert dbutil.run(dbutil.can_read_document(reporter.id, result.id)) is False
    # An unrelated caller holding only the operator scope can.
    assert (
        dbutil.run(dbutil.can_read_document(uuid5(), result.id, orgs=(settings.reports_scope_id,)))
        is True
    )


def test_report_deduplicates_an_identical_retry_into_the_same_document(
    fake_embedder: RecordingEmbedder,
    tools: dict[str, FunctionTool],
) -> None:
    reporter = _reporter()
    text = f"find returned nothing for {uuid5().hex}, a question the corpus plainly answers."

    first = dbutil.run(tools["report"].fn(text=text, context=context_for(reporter)))
    calls_after_first = len(fake_embedder.calls)
    second = dbutil.run(tools["report"].fn(text=text, context=context_for(reporter)))

    async def stored_copies() -> int:
        operator = _operator()
        async with operator as session:
            counted = await session.exec(
                sql("SELECT count(*) FROM document WHERE id = :id"), params={"id": first.id}
            )
            return counted.scalar_one()

    assert first == second
    assert dbutil.run(stored_copies()) == 1
    # An identical retry never re-embeds, the same guard `remember` already relies on.
    assert len(fake_embedder.calls) == calls_after_first


def test_report_refuses_a_caller_without_the_report_scope(
    caller_context: Context,
    tools: dict[str, FunctionTool],
) -> None:
    with pytest.raises(ToolError, match="may not write operator reports"):
        dbutil.run(
            tools["report"].fn(
                text="A confusing pair of settled facts.",
                context=caller_context,
            )
        )


def test_report_reports_invalid_self_describing_metadata_as_a_tool_error(
    tools: dict[str, FunctionTool],
) -> None:
    reporter = _reporter()

    with pytest.raises(ToolError, match="typed source text needs a level-one Markdown title"):
        dbutil.run(
            tools["report"].fn(
                text="- Type Project\n- has_status [Status] Active",
                context=context_for(reporter),
            )
        )


def test_report_shares_remembers_monthly_quota_bucket(
    monkeypatch: pytest.MonkeyPatch,
    tools: dict[str, FunctionTool],
) -> None:
    monkeypatch.setattr(settings, "monthly_user_remember_limit", 1)
    monkeypatch.setattr(
        memory_module.extract_ingest, "ingest_text", AsyncMock(return_value=uuid7())
    )
    monkeypatch.setattr(memory_module, "enqueue_document", AsyncMock(return_value=0))
    reporter = _reporter()

    dbutil.run(tools["keep"].fn(text="# First\n\nA kept note.", context=context_for(reporter)))

    with pytest.raises(ToolError, match="monthly remember limit reached"):
        dbutil.run(
            tools["report"].fn(
                text="A confusing pair of facts filed right after the limit was spent.",
                context=context_for(reporter),
            )
        )


def test_report_text_annotation_enforces_the_configured_cap() -> None:
    fn = tools_of(mcp_probe.server)["report"].fn
    annotation = get_type_hints(fn, include_extras=True)["text"]
    adapter = TypeAdapter(annotation)

    adapter.validate_python("x" * settings.mcp_report_max_chars)
    with pytest.raises(ValidationError):
        adapter.validate_python("x" * (settings.mcp_report_max_chars + 1))


def test_recall_names_the_report_scope_for_an_operator_reading_one_back(
    monkeypatch: pytest.MonkeyPatch,
    tools: dict[str, FunctionTool],
) -> None:
    """No Logto organization backs the report scope, so its evidence still needs a name.

    An operator's read authority covers the report scope, so evidence standing in it reaches
    the scope catalog. When that catalog could not name it, every `find` an operator ran that
    matched a filed report raised instead of answering.
    """
    operator = _operator()
    candidate = Candidate(
        lane=Lane.Kind.FACTS,
        line="an agent reported two settled facts that contradict each other",
        scopes=frozenset({settings.reports_scope_id}),
    )

    async def stub(query: str, user: User, token_budget: int | None = None) -> RecallEvidence:
        return RecallEvidence(candidates=(candidate,))

    monkeypatch.setattr(memory_module.retrieval, "evidence", stub)

    out = dbutil.run(tools["find"].fn(query="what did agents report", context=context_for(operator)))

    assert "reports" in out
    assert "an agent reported two settled facts that contradict each other" in out
