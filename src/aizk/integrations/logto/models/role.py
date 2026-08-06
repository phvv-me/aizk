from typing import Annotated

from patos import FrozenOpenModel
from pydantic import StringConstraints


class Role(FrozenOpenModel):
    """API or organization role assigned to one Logto user."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    description: str | None = None
