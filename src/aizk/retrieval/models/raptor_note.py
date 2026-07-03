from patos import FrozenModel


class RaptorNote(FrozenModel):
    """One RAPTOR tree summary surfaced for a query, a higher tier of the global view.

    Where a community summary covers one detected cluster, a RAPTOR summary rolls several of those
    up, so the root summaries answer the broadest queries and the leaf summaries the narrower ones.

    label: short name of the theme the summary covers.
    summary: paragraph rolling up the level below it.
    level: the tree level the summary sits at, higher being broader.
    score: similarity of the summary to the query, higher is better.
    """

    label: str
    summary: str
    level: int
    score: float
