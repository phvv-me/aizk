from patos import FrozenModel

from .observation import Observation


class InsightReport(FrozenModel):
    """The reflective pass's report, the candidate observations before the significance gate.

    observations: the derived insights, each carrying its own significance the pass filters on.
    """

    observations: list[Observation]
