import types
import uuid
from datetime import datetime

import pytest
from factories import FactHitFactory, RecallResultFactory

from aizk.retrieval import RecallResult


class FakeMeter:
    """A deterministic stand-in for the mainboard meter, fixed peaks and a no-op sampler.

    Reads the same host and GPU peak every run without probing a real host, so the report
    assertions stay stable while the live meter still measures a true peak in production. This is
    the one external boundary the offline sweep and scale tests replace.
    """

    peak_host_gb = 1.5
    peak_gpu_gb = 0.5

    def __enter__(self) -> FakeMeter:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def sample(self) -> None:
        """Record nothing, the live meter's per-recall memory reading stubbed out."""


def fact_bundle(query: str, statements: list[str]) -> RecallResult:
    """A recall bundle carrying the given fact statements and nothing else in any other lane.

    query: the question the bundle answers.
    statements: the fact statements the bundle surfaces, ranked ahead of any passage.
    """
    return RecallResultFactory.build(
        query=query,
        facts=[FactHitFactory.build(statement=statement) for statement in statements],
        hits=[],
        communities=[],
        raptor=[],
        session=[],
        profile=None,
        as_of=None,
    )


def install_constant_recall(
    monkeypatch: pytest.MonkeyPatch, module: types.ModuleType, statement: str
) -> None:
    """Stub the module's imported `recall` to a bundle surfacing one fixed fact, the scoring seam.

    monkeypatch: the fixture the stub installs through.
    module: the eval module whose imported `recall` name is replaced.
    statement: the single fact statement every stubbed recall returns.
    """

    async def stub_recall(
        query: str,
        principal_id: uuid.UUID | None = None,
        k: int = 8,
        as_of: datetime | None = None,
    ) -> RecallResult:
        return fact_bundle(query, [statement])

    monkeypatch.setattr(module, "recall", stub_recall)


def install_fake_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the sweep's `open_meter` onto the fixed-peak fake, so no host is probed.

    monkeypatch: the fixture the stub installs through.
    """
    import aizk.eval.sweep as sweep_module

    monkeypatch.setattr(sweep_module, "open_meter", FakeMeter)
