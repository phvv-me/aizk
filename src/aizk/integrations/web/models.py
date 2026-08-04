from datetime import datetime
from enum import StrEnum, auto

from patos import FrozenModel, Model
from pydantic import Field, NonNegativeInt
from pydantic.networks import AnyHttpUrl


class SearchLane(StrEnum):
    """Which kind of external index a sanitized query is sent to."""

    keyword = auto()
    semantic = auto()
    none = auto()


class Freshness(StrEnum):
    """How fast the answer to a question goes stale, which is the cache lifetime."""

    stable = auto()
    dated = auto()
    volatile = auto()


class WebResult(FrozenModel):
    """One search hit a provider returned, before any page has been fetched.

    Search rows are vendor output. Some vendors licence them for display only, so nothing
    here is ever written to the store on its own. A row becomes storable knowledge only
    once a fetcher that may persist has read the page behind it.
    """

    url: AnyHttpUrl
    title: str | None = None
    snippet: str | None = None
    published_at: datetime | None = None

    @property
    def preview(self) -> str:
        """The hit rendered as one line, the text used when no page could be fetched."""
        return "\n".join(part for part in (self.title, self.snippet) if part) or str(self.url)


class WebPage(FrozenModel):
    """One fetched page as Markdown, carrying the licence flag the cache enforces."""

    url: AnyHttpUrl
    markdown: str
    provider: str
    retrieved_at: datetime
    title: str | None = None
    persistable: bool = Field(
        default=False,
        description="whether the serving provider's licence permits storing this text",
    )

    def trimmed(self, max_chars: int) -> str:
        """The page text cut to a rendering bound, marked when the cut removed anything."""
        if len(self.markdown) <= max_chars:
            return self.markdown
        return self.markdown[:max_chars] + "…"


class ProviderTraffic(Model):
    """The wire sizes of the last external call one provider made.

    The provider fills this in from the request and response it actually sent and received,
    so the ledger records what the wire carried rather than what the caller guessed it
    would. It is mutable on purpose, because it is a measurement of a call rather than part
    of the provider's configuration.
    """

    request_bytes: NonNegativeInt = 0
    response_bytes: NonNegativeInt = 0

    def record(self, request_bytes: int, response_bytes: int) -> None:
        """Replace the sizes with those of the call that just finished."""
        self.request_bytes = request_bytes
        self.response_bytes = response_bytes


class ProviderUnavailable(RuntimeError):
    """One provider is unconfigured, refused the call, or answered unusably.

    Never fatal on its own. The lane chain skips the provider and tries the next one, and a
    chain where every provider raises this degrades the whole call to memory alone.
    """
