from patos import FrozenModel


class QA(FrozenModel):
    """One evaluation item, a question paired with the fact it must surface when one is known.

    question: the natural-language query handed to recall.
    expected: the source fact that must appear in recall, null for a caller's own question.
    """

    question: str
    expected: str | None
