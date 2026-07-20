import json
import sys
from io import TextIOBase

from patos import FrozenModel


class ResultSerializer:
    """Render command results through one deterministic JSON policy."""

    @staticmethod
    def json(result: FrozenModel | str) -> str:
        """Return indented, key-sorted JSON for a model or Markdown string."""
        serializable = result if isinstance(result, str) else result.model_dump(mode="json")
        return json.dumps(serializable, indent=2, sort_keys=True)


class CommandInput:
    """Resolve optional positional text without blocking an interactive terminal."""

    @staticmethod
    def text(value: str | None, stream: TextIOBase | None = None) -> str | None:
        """Prefer explicit text and read stdin only when it is a pipe."""
        if value is not None:
            return value
        source = stream or sys.stdin
        return None if source.isatty() else source.read()
