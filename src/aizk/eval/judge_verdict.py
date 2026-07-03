from patos import FrozenModel


class JudgeVerdict(FrozenModel):
    """The judge's call on whether a recalled context answers a question.

    answerable: whether the recalled context holds enough to answer the question.
    """

    answerable: bool
