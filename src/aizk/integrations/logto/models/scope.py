from typing import Annotated

from patos import FrozenOpenModel
from pydantic import StringConstraints


class OrganizationScope(FrozenOpenModel):
    """One organization permission returned directly by Logto."""

    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    description: str | None = None
