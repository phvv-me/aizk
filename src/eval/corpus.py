import hashlib
from collections.abc import Sequence
from pathlib import Path

from patos import FrozenModel
from pydantic import TypeAdapter

from aizk.store.identity import User

from .plans import Stratum, StudyQuestion, stratum_questions

DEFAULT_PER_STRATUM = 100
FROZEN_CORPUS_PATH = Path("tests/benchmark/data/retrieval_questions.jsonl")
_CORPUS_VERSION = "1"
_QUESTION = TypeAdapter(StudyQuestion)


class FrozenStudyCorpus(FrozenModel):
    """A fingerprinted immutable question set used by retrieval benchmarks."""

    path: Path
    fingerprint: str
    questions: tuple[StudyQuestion, ...]

    def render(self) -> str:
        """Render the frozen corpus location, size, and full SHA-256."""
        return (
            f"frozen retrieval corpus n={len(self.questions)} "
            f"sha256={self.fingerprint} path={self.path}"
        )


def fingerprint_path(path: Path) -> Path:
    """Return the committed fingerprint companion for one JSONL corpus."""
    return path.with_suffix(f"{path.suffix}.sha256")


def corpus_fingerprint(questions: Sequence[StudyQuestion]) -> str:
    """Hash validated questions and the adapter version in stored order."""
    digest = hashlib.sha256(_CORPUS_VERSION.encode())
    for question in questions:
        digest.update(question.model_dump_json().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def load_frozen_corpus(path: Path = FROZEN_CORPUS_PATH) -> FrozenStudyCorpus:
    """Load a committed corpus only when its computed fingerprint matches."""
    questions = tuple(
        _QUESTION.validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()
    )
    expected = fingerprint_path(path).read_text(encoding="utf-8").strip()
    actual = corpus_fingerprint(questions)
    if actual != expected:
        raise ValueError(f"frozen corpus fingerprint mismatch: expected {expected}, got {actual}")
    return FrozenStudyCorpus(path=path, fingerprint=actual, questions=questions)


async def freeze_corpus(
    path: Path,
    user: User,
    per_stratum: int = DEFAULT_PER_STRATUM,
    strata: Sequence[Stratum] = tuple(Stratum),
) -> FrozenStudyCorpus:
    """Generate each selected stratum once and commit JSONL plus its fingerprint."""
    questions: list[StudyQuestion] = []
    for stratum in strata:
        generated = await stratum_questions(stratum, user, per_stratum)
        if len(generated) != per_stratum:
            raise ValueError(
                f"{stratum.value} generated {len(generated)} questions, expected {per_stratum}"
            )
        questions.extend(
            question.model_copy(update={"id": f"{stratum.value}:{index:04d}"})
            for index, question in enumerate(generated)
        )
    fingerprint = corpus_fingerprint(questions)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{question.model_dump_json()}\n" for question in questions),
        encoding="utf-8",
    )
    fingerprint_path(path).write_text(f"{fingerprint}\n", encoding="utf-8")
    return FrozenStudyCorpus(
        path=path,
        fingerprint=fingerprint,
        questions=tuple(questions),
    )
