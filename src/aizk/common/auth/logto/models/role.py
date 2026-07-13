from typing import Annotated

from patos import FrozenModel
from pydantic import StringConstraints


class Role(FrozenModel):
    """Organization role assigned to one Logto member."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
