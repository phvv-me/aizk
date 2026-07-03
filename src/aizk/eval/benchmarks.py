from collections.abc import Callable
from pathlib import Path

import jsonlines
from patos import FrozenModel

from .qa import QA


class BenchmarkItem(FrozenModel):
    """One benchmark question with its gold answers and the temporal or scope context it tests.

    The scope field carries EverMemBench's multi-party owner, the speaker or team a memory belongs
    to, the signal our scope promotion reads and the other benchmarks drop. The valid-time fields
    carry TEMPO's bi-temporal window, the world-time the answer holds over, so a recall as_of that
    window is what the benchmark scores rather than the latest state.

    question: the natural-language query handed to recall.
    answers: the gold answer texts a recalled context must surface, the relevant set.
    scope: the multi-party owner the memory belongs to, null for a single-party item.
    valid_from: start of the world-time window the answer holds over, null when timeless.
    valid_to: end of the world-time window the answer holds over, null while still holding.
    """

    question: str
    answers: list[str]
    scope: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None

    def to_qa(self) -> QA:
        """Render this item as the harness QA, the first gold answer as the expected fact."""
        return QA(question=self.question, expected=self.answers[0] if self.answers else None)


def load_jsonl(path: Path, builder: Callable[[dict], BenchmarkItem]) -> list[BenchmarkItem]:
    """Read a JSONL dataset and build one benchmark item per line through the given mapper.

    path: the JSONL file, one benchmark record per line.
    builder: maps a parsed record to a benchmark item, the per-dataset field mapping.
    """
    with jsonlines.open(path) as reader:
        return [builder(record) for record in reader]


def answers_of(record: dict) -> list[str]:
    """Read the gold answers of a record, accepting the `answers` list or a single `answer`.

    record: one parsed benchmark record.
    """
    if "answers" in record:
        return [str(answer) for answer in record["answers"]]
    return [str(record["answer"])]


def evermembench_item(record: dict) -> BenchmarkItem:
    """Map one EverMemBench record to a benchmark item, carrying its multi-party scope.

    EverMemBench is collaborative memory across several speakers, so each record names the scope
    the memory belongs to, the team or speaker our scope promotion reads, kept here so the sweep
    exercises the cross-party promotion the single-party benchmarks ignore.

    record: one parsed EverMemBench record carrying question, answers, and scope.
    """
    scope = record.get("scope") or record.get("group") or record.get("speaker")
    return BenchmarkItem(
        question=str(record["question"]),
        answers=answers_of(record),
        scope=str(scope) if scope is not None else None,
    )


def tempo_item(record: dict) -> BenchmarkItem:
    """Map one TEMPO record to a benchmark item, carrying its bi-temporal validity window.

    TEMPO scores recall against the world-time an answer holds over, so each record names the
    valid-from and valid-to bounds the answer is true between, kept here so the sweep can recall
    as_of that window rather than only the latest state.

    record: one parsed TEMPO record carrying question, answers, and a valid-time window.
    """
    valid_from = record.get("valid_from") or record.get("start")
    valid_to = record.get("valid_to") or record.get("end")
    return BenchmarkItem(
        question=str(record["question"]),
        answers=answers_of(record),
        valid_from=str(valid_from) if valid_from is not None else None,
        valid_to=str(valid_to) if valid_to is not None else None,
    )


def load_evermembench(path: Path) -> list[BenchmarkItem]:
    """Load the EverMemBench multi-party collaborative-memory dataset from JSONL.

    path: the EverMemBench JSONL file.
    """
    return load_jsonl(path, evermembench_item)


def load_tempo(path: Path) -> list[BenchmarkItem]:
    """Load the TEMPO bi-temporal dataset from JSONL.

    path: the TEMPO JSONL file.
    """
    return load_jsonl(path, tempo_item)


def benchmark_gold(items: list[BenchmarkItem]) -> list[QA]:
    """Render benchmark items as the harness gold the sweep and eval score against.

    items: the loaded benchmark items.
    """
    return [item.to_qa() for item in items]


# the named benchmark loaders, registered so an admin tool selects one by name rather than a branch
LOADERS: dict[str, Callable[[Path], list[BenchmarkItem]]] = {
    "evermembench": load_evermembench,
    "tempo": load_tempo,
}
