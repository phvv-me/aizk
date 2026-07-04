import json
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from aizk.eval.benchmarks import (
    LOADERS,
    BenchmarkItem,
    answers_of,
    benchmark_gold,
    load_evermembench,
    load_jsonl,
    load_tempo,
)

# a tiny EverMemBench fixture, two collaborative-memory questions each owned by a party scope, the
# multi-party signal our scope promotion reads and the other benchmarks drop.
EVERMEMBENCH_LINES = [
    {
        "question": "what did the team decide on the launch",
        "answers": ["ship in march"],
        "scope": "team-rocket",
    },
    {"question": "who owns the billing rewrite", "answer": "dana", "speaker": "dana"},
    {"question": "which group planned it", "answers": ["ops"], "group": "ops-guild"},
]

# a tiny TEMPO fixture, one bi-temporal question whose answer holds only over a world-time window,
# the validity bounds a temporal recall reads.
TEMPO_LINES = [
    {
        "question": "who was the lead in early 2026",
        "answers": ["mina"],
        "valid_from": "2026-01-01",
        "valid_to": "2026-04-01",
    },
    {"question": "what was the model served then", "answer": "qwen3", "start": "2026-02-01"},
]


def write_jsonl(path: Path, records: list[dict]) -> Path:
    """Write one JSON record per line to a file, the on-disk shape the loaders read.

    path: the file to write.
    records: the benchmark records, one per line.
    """
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return path


def test_load_evermembench_carries_the_multi_party_scope(tmp_path: Path) -> None:
    """Each EverMemBench item keeps its owning scope, the cross-party signal the sweep reads."""
    items = load_evermembench(write_jsonl(tmp_path / "ever.jsonl", EVERMEMBENCH_LINES))

    assert len(items) == 3
    assert all(isinstance(item, BenchmarkItem) for item in items)
    assert items[0].scope == "team-rocket"
    assert items[0].answers == ["ship in march"]
    # the single-answer alias and the speaker-as-scope fallback both resolve
    assert items[1].answers == ["dana"]
    assert items[1].scope == "dana"
    # the group alias is the last scope fallback the mapper reaches for
    assert items[2].scope == "ops-guild"


def test_load_tempo_carries_the_bitemporal_window(tmp_path: Path) -> None:
    """Each TEMPO item keeps its valid-time bounds, the world-time the answer holds over."""
    items = load_tempo(write_jsonl(tmp_path / "tempo.jsonl", TEMPO_LINES))

    assert len(items) == 2
    assert (items[0].valid_from, items[0].valid_to) == ("2026-01-01", "2026-04-01")
    assert items[0].answers == ["mina"]
    # the start alias fills valid_from and an absent end leaves valid_to null
    assert items[1].valid_from == "2026-02-01"
    assert items[1].valid_to is None
    # tempo carries no scope
    assert items[1].scope is None


def test_benchmark_gold_maps_the_first_answer_to_the_expected_fact(tmp_path: Path) -> None:
    """The first gold answer becomes the expected fact the sweep scores a recall hit against."""
    items = load_tempo(write_jsonl(tmp_path / "tempo.jsonl", TEMPO_LINES))

    gold = benchmark_gold(items)

    assert [qa.question for qa in gold] == [item.question for item in items]
    assert gold[0].expected == "mina"


def test_loaders_registry_names_both_benchmarks() -> None:
    """The registry exposes both 2026 benchmarks by name, the key an admin tool selects on."""
    assert set(LOADERS) == {"evermembench", "tempo"}
    assert LOADERS["evermembench"] is load_evermembench
    assert LOADERS["tempo"] is load_tempo


def test_to_qa_handles_an_answerless_item() -> None:
    """An item carrying no gold answer maps to a question whose expected fact is null."""
    qa = BenchmarkItem(question="open question", answers=[]).to_qa()

    assert (qa.question, qa.expected) == ("open question", None)


@given(
    answers=st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=4),
    single=st.booleans(),
)
def test_answers_of_reads_the_list_or_single_alias(answers: list[str], single: bool) -> None:
    """A record's gold set reads from the `answers` list or the single `answer`, always strings."""
    record = {"answer": answers[0]} if single else {"answers": answers}

    read = answers_of(record)

    assert read == ([answers[0]] if single else [str(answer) for answer in answers])
    assert all(isinstance(answer, str) for answer in read)


@given(
    records=st.lists(
        st.dictionaries(st.text(), st.integers(min_value=-(2**63), max_value=2**63 - 1)),
        max_size=6,
    )
)
def test_load_jsonl_builds_one_item_per_line_in_order(tmp_path: Path, records: list[dict]) -> None:
    """The streaming reader yields exactly one built item per JSONL line, in file order.

    Integers stay within int64: JSON parsers only agree on interoperable range, and the loader's
    own parser coerces anything wider to float, a boundary real benchmark data never crosses.
    """
    path = write_jsonl(tmp_path / "raw.jsonl", records)
    seen: list[dict] = []

    def builder(record: dict) -> BenchmarkItem:
        seen.append(record)
        return BenchmarkItem(question=str(len(seen)), answers=[])

    items = load_jsonl(path, builder)

    assert len(items) == len(records)
    assert seen == records
    assert [item.question for item in items] == [str(i + 1) for i in range(len(records))]
