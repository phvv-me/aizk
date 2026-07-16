from typing import ClassVar

from patos import FrozenModel

from aizk import ops
from aizk.config import settings


class EvaluationDatabase(FrozenModel):
    """Own destructive setup for a database reserved for isolated evaluation."""

    suffix: ClassVar[str] = "_eval"

    async def reset(self) -> None:
        """Recreate the configured evaluation database and install the complete schema."""
        if not settings.db_name.endswith(self.suffix):
            raise ValueError(
                f"isolated evaluation requires a database ending in {self.suffix!r}, "
                f"got {settings.db_name!r}"
            )
        await ops.reset()
