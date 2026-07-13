from enum import StrEnum, auto

from ..config import settings
from . import ontology
from .llm import combined_extract, extract_with_system
from .llm.triples import extraction_system
from .models import Extraction


class ExtractionStrategy(StrEnum):
    """The graph detail level applied to one extraction call."""

    ONTOLOGY = auto()
    SUMMARY = auto()
    PREFERENCES = auto()
    CUSTOM = auto()

    def system(self) -> str:
        """Build this strategy's system prompt over the live ontology."""
        match self:
            case self.SUMMARY:
                focus = settings.extract_summary_prompt
            case self.PREFERENCES:
                focus = settings.extract_preferences_prompt
            case self.CUSTOM:
                focus = settings.extract_custom_prompt
            case _:
                return extraction_system()
        return f"{ontology.current().prompt}\n{focus}" if focus else extraction_system()

    async def extract(self, text: str) -> Extraction:
        """Extract one graph slice under this strategy."""
        if self is self.ONTOLOGY or self is self.CUSTOM and not settings.extract_custom_prompt:
            return await combined_extract(text)
        return await extract_with_system(self.system(), text)


async def extract_graph(text: str) -> Extraction:
    """Extract a graph slice with the configured typed strategy."""
    return await ExtractionStrategy(settings.extract_strategy).extract(text)
