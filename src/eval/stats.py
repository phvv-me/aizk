import math
from collections.abc import Sequence
from typing import Literal

import numpy as np
from patos import FrozenModel
from scipy.stats import permutation_test
from statsmodels.stats.multitest import multipletests

Alternative = Literal["less", "greater"]
Samples = Sequence[float] | np.ndarray


class SNRDiagnostic(FrozenModel):
    """Arm separation, paired-bootstrap run noise, and their ratio."""

    signal: float
    noise: float
    ratio: float


def _paired_vectors(
    first: Samples,
    second: Samples,
) -> tuple[np.ndarray, np.ndarray]:
    first_array = np.asarray(first, dtype=float)
    second_array = np.asarray(second, dtype=float)
    if first_array.ndim != 1 or second_array.ndim != 1:
        raise ValueError("paired samples must be one-dimensional")
    if first_array.shape != second_array.shape or not first_array.size:
        raise ValueError("paired samples must be nonempty and equal length")
    return first_array, second_array


def paired_permutation_pvalue(
    first: Samples,
    second: Samples,
    *,
    alternative: Alternative,
    seed: int = 0,
    n_resamples: int = 9_999,
) -> float:
    """Return a seeded one-sided paired permutation p-value for mean difference."""
    first_array, second_array = _paired_vectors(first, second)

    def mean_difference(left: np.ndarray, right: np.ndarray) -> float:
        return float(np.mean(left - right))

    result = permutation_test(
        (first_array, second_array),
        mean_difference,
        permutation_type="samples",
        vectorized=False,
        n_resamples=n_resamples,
        alternative=alternative,
        rng=np.random.default_rng(seed),
    )
    return float(result.pvalue)


def holm_rejections(
    pvalues: Sequence[float],
    alpha: float = 0.05,
) -> tuple[bool, ...]:
    """Apply Holm family-wise error correction to one comparison family."""
    values = np.asarray(pvalues, dtype=float)
    if values.ndim != 1 or not values.size:
        raise ValueError("Holm correction requires a nonempty p-value vector")
    rejected = multipletests(values, alpha=alpha, method="holm")[0]
    return tuple(bool(value) for value in rejected)


def paired_cohens_dz(
    first: Samples,
    second: Samples,
) -> float:
    """Return paired Cohen's dz for `first - second`."""
    first_array, second_array = _paired_vectors(first, second)
    differences = first_array - second_array
    mean = float(np.mean(differences))
    deviation = float(np.std(differences, ddof=1)) if differences.size > 1 else 0.0
    if deviation:
        return mean / deviation
    return 0.0 if mean == 0.0 else math.copysign(math.inf, mean)


def signal_to_noise(
    arms: Sequence[Samples],
    *,
    seed: int = 0,
    n_resamples: int = 2_000,
) -> SNRDiagnostic:
    """Estimate arm separation over paired-bootstrap aggregate run noise."""
    try:
        matrix = np.asarray([tuple(arm) for arm in arms], dtype=float)
    except TypeError as error:
        raise ValueError("SNR requires at least two arms and two paired observations") from error
    if matrix.ndim != 2 or matrix.shape[0] < 2 or matrix.shape[1] < 2:
        raise ValueError("SNR requires at least two arms and two paired observations")
    if n_resamples < 2:
        raise ValueError("SNR requires at least two bootstrap resamples")
    means = np.mean(matrix, axis=1)
    signal = float(np.ptp(means))
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, matrix.shape[1], size=(n_resamples, matrix.shape[1]))
    bootstrap_means = matrix[:, indices].mean(axis=2)
    noise = float(np.mean(np.std(bootstrap_means, axis=1, ddof=1)))
    ratio = signal / noise if noise else (math.inf if signal else 0.0)
    return SNRDiagnostic(signal=signal, noise=noise, ratio=ratio)
