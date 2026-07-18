from typing import Annotated

from patos import FrozenModel
from pydantic import StringConstraints


class Role(FrozenModel):
    """API or organization role assigned to one Logto user."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    description: str | None = None
