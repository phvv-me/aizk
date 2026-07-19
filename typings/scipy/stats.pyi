from collections.abc import Callable
from typing import Any, Literal, Protocol

class PermutationTestResult(Protocol):
    pvalue: float

def permutation_test(
    data: tuple[Any, ...],
    statistic: Callable[..., float],
    *,
    permutation_type: Literal["independent", "samples", "pairings"] = ...,
    vectorized: bool = ...,
    n_resamples: int = ...,
    alternative: Literal["two-sided", "less", "greater"] = ...,
    rng: object = ...,
) -> PermutationTestResult: ...
