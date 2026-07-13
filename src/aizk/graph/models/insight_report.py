from patos import FrozenModel

from .observation import Observation


class InsightReport(FrozenModel):
    """The reflective pass's report, its candidate observations derived from stored facts,
    before the significance gate filters out the low-value ones."""

    observations: list[Observation]
