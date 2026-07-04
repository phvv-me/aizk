from patos import FrozenModel

from .block import Block


class ContextPack(FrozenModel):
    """A token-budgeted, prompt-ready slice of a recall bundle, `get_context`'s structured return.

    query: the natural-language query this pack answers.
    blocks: the kept blocks, broad view first and raw sources last, each tagged with the lane it
        came from; a caller wanting plain text groups them by `lane` in this same order.
    budget: the token ceiling the pack was fit to.
    used_tokens: the estimated token cost of the kept blocks, always at or under budget.
    """

    query: str
    blocks: list[Block]
    budget: int
    used_tokens: int
