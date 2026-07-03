from patos import FrozenModel


class SessionNote(FrozenModel):
    """One still-working session item recall folds in beside the graph, the working-memory lane.

    A remembered item lives here until the promotion pass moves its knowledge into the graph, so
    recall surfaces the recent captures a query matches before they have become facts, the fast
    front of memory the graph lanes cannot yet answer from.

    text: the remembered content.
    kind: the coarse type tag the item was captured under.
    score: cosine similarity of the item to the query, one minus its distance.
    """

    text: str
    kind: str
    score: float
