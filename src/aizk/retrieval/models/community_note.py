from patos import FrozenModel


class CommunityNote(FrozenModel):
    """One community summary surfaced for a thematic query, the global view recall folds in.

    label: short name of the cluster the summary covers.
    summary: paragraph describing what the cluster's entities and facts are about.
    score: similarity of the summary to the query, higher is better.
    """

    label: str
    summary: str
    score: float
