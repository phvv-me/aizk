from pathlib import Path

import dbutil
import pytest

import eval.corpus as corpus_module
from aizk.store.identity import User
from eval.corpus import (
    corpus_fingerprint,
    fingerprint_path,
    freeze_corpus,
    load_frozen_corpus,
)
from eval.plans import Stratum, StudyQuestion


def question(stratum: Stratum, index: int) -> StudyQuestion:
    return StudyQuestion(
        question=f"q-{stratum.value}-{index}",
        expected=(f"e-{stratum.value}-{index}",),
        stratum=stratum,
    )


def test_freeze_writes_stable_ids_jsonl_and_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def generate(
        stratum: Stratum,
        user: User,
        count: int,
    ) -> list[StudyQuestion]:
        del user
        return [question(stratum, index) for index in range(count)]

    monkeypatch.setattr(corpus_module, "stratum_questions", generate)
    path = tmp_path / "questions.jsonl"
    frozen = dbutil.run(
        freeze_corpus(
            path,
            User.system(),
            per_stratum=2,
            strata=(Stratum.LOCAL, Stratum.GLOBAL),
        )
    )
    loaded = load_frozen_corpus(path)

    assert loaded == frozen
    assert [row.id for row in loaded.questions] == [
        "local:0000",
        "local:0001",
        "global:0000",
        "global:0001",
    ]
    assert fingerprint_path(path).read_text(encoding="utf-8").strip() == frozen.fingerprint
    assert corpus_fingerprint(loaded.questions) == frozen.fingerprint
    assert frozen.render() == (
        f"frozen retrieval corpus n=4 sha256={frozen.fingerprint} path={path}"
    )


def test_load_rejects_fingerprint_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def generate(
        stratum: Stratum,
        user: User,
        count: int,
    ) -> list[StudyQuestion]:
        del user
        return [question(stratum, index) for index in range(count)]

    monkeypatch.setattr(corpus_module, "stratum_questions", generate)
    path = tmp_path / "questions.jsonl"
    dbutil.run(freeze_corpus(path, User.system(), 1, (Stratum.LOCAL,)))
    path.write_text(
        path.read_text(encoding="utf-8").replace("q-local-0", "changed"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        load_frozen_corpus(path)


def test_freeze_requires_the_requested_stratum_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def generate(
        stratum: Stratum,
        user: User,
        count: int,
    ) -> list[StudyQuestion]:
        del stratum, user, count
        return []

    monkeypatch.setattr(corpus_module, "stratum_questions", generate)

    with pytest.raises(ValueError, match="local generated 0 questions, expected 1"):
        dbutil.run(
            freeze_corpus(
                tmp_path / "questions.jsonl",
                User.system(),
                1,
                (Stratum.LOCAL,),
            )
        )
