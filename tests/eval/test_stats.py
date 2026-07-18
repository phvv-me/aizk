import math

import pytest

from eval.stats import (
    holm_rejections,
    paired_cohens_dz,
    paired_permutation_pvalue,
    signal_to_noise,
)


def test_paired_permutation_is_one_sided_and_seeded() -> None:
    first = [0.0] * 20
    second = [float(index) for index in range(1, 21)]

    first_run = paired_permutation_pvalue(
        first,
        second,
        alternative="less",
        seed=7,
        n_resamples=127,
    )
    second_run = paired_permutation_pvalue(
        first,
        second,
        alternative="less",
        seed=7,
        n_resamples=127,
    )

    assert first_run == second_run
    assert first_run < 0.05


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ([[1.0]], [1.0]),
        ([1.0], [[1.0]]),
        ([1.0], [1.0, 2.0]),
        ([], []),
    ],
)
def test_paired_statistics_reject_invalid_vectors(
    first: list[float] | list[list[float]],
    second: list[float] | list[list[float]],
) -> None:
    with pytest.raises(ValueError, match="paired samples"):
        paired_cohens_dz(first, second)  # type: ignore[arg-type]


def test_paired_cohens_dz_handles_variable_and_constant_differences() -> None:
    assert paired_cohens_dz([2.0, 4.0], [1.0, 1.0]) == pytest.approx(math.sqrt(2.0))
    assert paired_cohens_dz([1.0, 1.0], [1.0, 1.0]) == 0.0
    assert paired_cohens_dz([2.0], [1.0]) == math.inf


def test_holm_correction_rejects_only_adjusted_significance() -> None:
    assert holm_rejections([0.001, 0.02, 0.5]) == (True, True, False)


@pytest.mark.parametrize("pvalues", [[], [[0.1]]])
def test_holm_correction_requires_one_nonempty_dimension(
    pvalues: list[float] | list[list[float]],
) -> None:
    with pytest.raises(ValueError, match="Holm"):
        holm_rejections(pvalues)  # type: ignore[arg-type]


def test_snr_reports_separation_over_seeded_bootstrap_noise() -> None:
    diagnostic = signal_to_noise(
        ([0.0, 1.0, 0.0, 1.0], [1.0, 2.0, 1.0, 2.0]),
        seed=5,
        n_resamples=32,
    )

    assert diagnostic.signal == 1.0
    assert diagnostic.noise > 0.0
    assert diagnostic.ratio == pytest.approx(diagnostic.signal / diagnostic.noise)


def test_snr_defines_constant_signal_and_noise_edges() -> None:
    separated = signal_to_noise(([0.0, 0.0], [1.0, 1.0]), n_resamples=2)
    equal = signal_to_noise(([1.0, 1.0], [1.0, 1.0]), n_resamples=2)

    assert separated.ratio == math.inf
    assert equal.ratio == 0.0


@pytest.mark.parametrize(
    "arms",
    [
        [1.0, 2.0],
        [[1.0, 2.0]],
        [[1.0], [2.0]],
    ],
)
def test_snr_requires_two_arms_and_observations(
    arms: list[float] | list[list[float]],
) -> None:
    with pytest.raises(ValueError, match="SNR requires"):
        signal_to_noise(arms)  # type: ignore[arg-type]


def test_snr_requires_multiple_resamples() -> None:
    with pytest.raises(ValueError, match="bootstrap"):
        signal_to_noise(([1.0, 2.0], [2.0, 3.0]), n_resamples=1)
