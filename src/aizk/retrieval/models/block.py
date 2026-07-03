from patos import FrozenModel


class Block(FrozenModel):
    """One line of a recall bundle laid out for the context pack, tagged by the lane it came from.

    lane: the recall lane this line renders, the header the pack groups same-lane lines under.
    line: the rendered text of one item from that lane.
    """

    lane: str
    line: str
