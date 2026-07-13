import types
from importlib import import_module

import pytest
from factories import CandidateFactory

from aizk.retrieval import Candidate, Lane, Plan
from aizk.store.identity import User

_sweep_module = import_module("aizk.eval.sweep")


class FakeMeter:
    peak_host_gb = 1.5
    peak_gpu_gb = 0.5

    def __enter__(self) -> FakeMeter:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def sample(self) -> None:
        pass


def fact_bundle(statements: list[str]) -> tuple[Candidate, ...]:
    return tuple(
        CandidateFactory.build(lane=Lane.Kind.FACTS, line=statement) for statement in statements
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


def install_fake_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sweep_module, "open_meter", FakeMeter)
