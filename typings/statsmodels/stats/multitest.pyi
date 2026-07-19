from collections.abc import Sequence

def multipletests(
    pvals: object,
    alpha: float = ...,
    method: str = ...,
    is_sorted: bool = ...,
    returnsorted: bool = ...,
) -> tuple[Sequence[bool], Sequence[float], float, float]: ...
