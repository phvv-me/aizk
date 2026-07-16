import types

import pytest
from factories import CandidateFactory

from aizk.retrieval import Candidate, Lane, Plan
from aizk.store.identity import User


def fact_bundle(statements: list[str]) -> tuple[Candidate, ...]:
    return tuple(
        CandidateFactory.build(lane=Lane.Kind.FACTS, line=statement, scopes=frozenset())
        for statement in statements
    )


def install_constant_recall(
    monkeypatch: pytest.MonkeyPatch, module: types.ModuleType, statement: str
) -> None:
    async def stub_recall(
        query: str,
        user: User,
        k: int = 8,
        token_budget: int | None = None,
        plan: Plan | None = None,
    ) -> tuple[Candidate, ...]:
        del query, user, k, token_budget, plan
        return fact_bundle([statement])

    monkeypatch.setattr(module, "recall", stub_recall)
