from collections.abc import Callable
from typing import cast

import pytest

from aizk.serving.extract.long_text import GraphResult, LongTextExtractor, TextWindow


def span(text: str, start: int, end: int, confidence: float) -> dict[str, int | str | float]:
    return {"text": text, "start": start, "end": end, "confidence": confidence}


def test_long_text_windows_overlap_on_word_boundaries() -> None:
    extractor = LongTextExtractor(window_size=4, overlap=2, batch_size=2)
    text = "zero one two three four five six"

    assert extractor.windows(text) == [
        TextWindow("zero one two three", 0),
        TextWindow("two three four five", 9),
        TextWindow("four five six", 19),
    ]
    assert extractor.windows("one two") == [TextWindow("one two", 0)]


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda: LongTextExtractor(window_size=0, overlap=0, batch_size=1), "window_size"),
        (lambda: LongTextExtractor(window_size=2, overlap=2, batch_size=1), "window_size"),
        (lambda: LongTextExtractor(window_size=2, overlap=3, batch_size=1), "window_size"),
        (lambda: LongTextExtractor(window_size=2, overlap=-1, batch_size=1), "window_size"),
        (
            lambda: LongTextExtractor.merge([TextWindow("one", 0)], []),
            "each text window",
        ),
    ],
    ids=["zero", "equal-overlap", "large-overlap", "negative-overlap", "result-count"],
)
def test_long_text_rejects_invalid_configuration_and_results(
    operation: Callable[[], LongTextExtractor | GraphResult], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        operation()


def test_long_text_extract_batches_and_merges_overlaps() -> None:
    class Model:
        def batch_extract(
            self,
            texts: list[str],
            schemas: str,
            batch_size: int,
            threshold: float,
            num_workers: int,
            format_results: bool,
            include_confidence: bool,
            include_spans: bool,
        ) -> list[GraphResult]:
            assert texts == ["zero one two three", "two three four"]
            assert schemas == "schema"
            assert (batch_size, threshold, num_workers) == (2, 0.4, 0)
            assert format_results and include_confidence and include_spans
            return [
                cast(
                    "GraphResult",
                    {
                        "entities": {"concept": [span("two", 9, 12, 0.6)]},
                        "relation_extraction": {
                            "uses": [
                                {
                                    "head": span("two", 9, 12, 0.6),
                                    "tail": span("three", 13, 18, 0.7),
                                }
                            ]
                        },
                    },
                ),
                cast(
                    "GraphResult",
                    {
                        "entities": {"concept": [span("two", 0, 3, 0.9)]},
                        "relation_extraction": {
                            "uses": [
                                {
                                    "head": span("two", 0, 3, 0.9),
                                    "tail": span("three", 4, 9, 0.8),
                                }
                            ]
                        },
                    },
                ),
            ]

    result = LongTextExtractor(window_size=4, overlap=2, batch_size=2).extract(
        Model(), "zero one two three four", "schema", 0.4
    )

    assert result == {
        "entities": {"concept": [span("two", 9, 12, 0.9)]},
        "relation_extraction": {
            "uses": [
                {
                    "head": span("two", 9, 12, 0.9),
                    "tail": span("three", 13, 18, 0.8),
                }
            ]
        },
    }


def test_long_text_merge_keeps_the_strongest_duplicate() -> None:
    stronger = cast(
        "GraphResult",
        {
            "entities": {"concept": [span("one", 0, 3, 0.9)]},
            "relation_extraction": {
                "uses": [
                    {
                        "head": span("one", 0, 3, 0.9),
                        "tail": span("two", 4, 7, 0.8),
                    }
                ]
            },
        },
    )
    weaker = cast(
        "GraphResult",
        {
            "entities": {"concept": [span("one", 0, 3, 0.4)]},
            "relation_extraction": {
                "uses": [
                    {
                        "head": span("one", 0, 3, 0.4),
                        "tail": span("two", 4, 7, 0.5),
                    }
                ]
            },
        },
    )

    merged = LongTextExtractor.merge(
        [TextWindow("one two", 0), TextWindow("one two", 0)],
        [stronger, weaker],
    )

    assert merged == stronger
