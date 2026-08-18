import types

import pytest
from factories import CandidateFactory

from aizk.retrieval import Candidate, Lane, Plan
from aizk.store.identity import User


def fact_bundle(statements: list[str]) -> tuple[Candidate, ...]:
    # Ungrounded fact lines carry no source document, so the rendered context stays the
    # statement the eval arms score.
    return tuple(
        CandidateFactory.build(
            lane=Lane.Kind.FACTS,
            line=statement,
            scopes=frozenset(),
            document_id=None,
            document_created_at=None,
        )
        for statement in statements
    )


def install_constant_find(
    monkeypatch: pytest.MonkeyPatch, module: types.ModuleType, statement: str
) -> None:
    async def stub_find(
        query: str,
        user: User,
        k: int = 8,
        token_budget: int | None = None,
        plan: Plan | None = None,
    ) -> tuple[Candidate, ...]:
        del query, user, k, token_budget, plan
        return fact_bundle([statement])

    monkeypatch.setattr(module, "find", stub_find)
