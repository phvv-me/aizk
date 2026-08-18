from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum, auto
from functools import cached_property
from typing import ClassVar, Self

from patos import FrozenModel
from pydantic import UUID5, UUID7, Field

from ...provenance import Stance
from ...store import Document
from ..templates import environment
from .candidate import Candidate
from .lane import Lane

_template = environment.get_template("find.md.j2")


class _Provenance(StrEnum):
    """Public evidence provenance independent of internal retrieval lanes."""

    SOURCE = auto()
    DERIVED = auto()
    SESSION = auto()
    WEB = auto()


class _Scope(FrozenModel):
    """One exact Logto scope represented in found evidence."""

    name: str
    description: str | None = None


class _Evidence(FrozenModel):
    """One merit-ordered evidence item with exact scope provenance."""

    provenance: _Provenance
    text: str
    stance: Stance = Field(
        default=Stance.settled,
        description="how settled a derived claim is, settled for everything else",
    )
    scopes: tuple[_Scope, ...] = ()
    resource_uri: str | None = None
    document_id: UUID7 | None = None
    document_created_at: datetime | None = None
    document_note: str | None = Field(
        default=None, description="the document line the template renders, priced by packing"
    )
    provider: str | None = Field(
        default=None, description="the third party that served a web result, absent for memory"
    )
    retrieved_at: datetime | None = Field(
        default=None, description="when a web result was fetched, absent for memory"
    )
    source_url: str | None = Field(
        default=None, description="the public address a web result came from"
    )


# A `ClassVar` naming a class is a value rather than a type to a checker, so callers that
# need to annotate with these models reach for the aliases instead of the class attributes.
type Evidence = _Evidence
type Provenance = _Provenance


class FindResult(FrozenModel):
    """Structured `find` result that can be serialized as JSON or rendered as Markdown.

    Memory evidence and web findings stay in separate collections because they carry
    different trust. Everything in `evidence` is something the caller or an organization
    they belong to put into memory, while everything in `web` is a stranger's page fetched
    during this call. `receipt` is the one line stating exactly what left the machine, and
    a `find` always carries one even when the answer came entirely from memory.
    """

    Provenance: ClassVar[type[_Provenance]] = _Provenance
    Scope: ClassVar[type[_Scope]] = _Scope
    Evidence: ClassVar[type[_Evidence]] = _Evidence

    evidence: tuple[_Evidence, ...] = Field(default=(), description="merit-ordered evidence")
    web: tuple[_Evidence, ...] = Field(
        default=(), description="pages fetched from the public web during this call"
    )
    receipt: str | None = Field(
        default=None, description="what left this machine, or why nothing did"
    )

    @classmethod
    def from_candidates(
        cls,
        candidates: Sequence[Candidate],
        scopes: Mapping[UUID5, _Scope] | None = None,
    ) -> Self:
        """Build a public result while keeping internal retrieval lanes private."""
        return cls(
            evidence=tuple(
                cls.Evidence(
                    provenance=(
                        cls.Provenance.WEB
                        if candidate.web_cache
                        else cls.Provenance.SOURCE
                        if candidate.lane is Lane.Kind.SOURCES
                        else cls.Provenance.SESSION
                        if candidate.lane is Lane.Kind.WORKING_MEMORY
                        else cls.Provenance.DERIVED
                    ),
                    source_url=(
                        Document.public_url(candidate.source_uri) if candidate.web_cache else None
                    ),
                    text=candidate.line,
                    stance=candidate.stance,
                    resource_uri=candidate.resource_uri,
                    document_id=candidate.document_id,
                    document_created_at=candidate.document_created_at,
                    document_note=candidate.document_note,
                    scopes=(
                        tuple(
                            scopes[scope]
                            for scope in sorted(
                                candidate.scopes,
                                key=lambda scope: scopes[scope].name,
                            )
                        )
                        if scopes is not None
                        else ()
                    ),
                )
                for candidate in candidates
            )
        )

    @cached_property
    def kept(self) -> tuple[_Evidence, ...]:
        """Evidence the caller or an organization they belong to put into memory."""
        return tuple(item for item in self.evidence if item.provenance is not _Provenance.WEB)

    @cached_property
    def from_the_web(self) -> tuple[_Evidence, ...]:
        """Every item that came from a stranger's page, whichever lane surfaced it.

        A cached page ordinary retrieval found is exactly as untrusted as one fetched during
        this call, so both render under the one warning. Leaving the cached ones among the
        caller's own notes would make a label in the middle of a list carry the whole weight
        of that distinction, which is more than a label can hold.
        """
        return (
            tuple(item for item in self.evidence if item.provenance is _Provenance.WEB) + self.web
        )

    @cached_property
    def unsettled(self) -> tuple[_Evidence, ...]:
        """Every kept item whose source did not state it outright.

        The template turns this into a standing instruction rather than a label, because a
        word beside a claim is easy to read past while a sentence naming the excerpt as the
        authority is not. A derived claim arrives as a clean assertion whatever the sentence
        behind it said, so this is the only place the reader is told which ones those are.
        """
        return tuple(item for item in self.kept if item.stance is not Stance.settled)

    @cached_property
    def shared_scopes(self) -> tuple[_Scope, ...]:
        """Return each shared scope represented by evidence once in name order."""
        by_name = {
            scope.name: scope
            for item in self.evidence
            for scope in item.scopes
            if scope.name != "private"
        }
        return tuple(by_name[name] for name in sorted(by_name))

    async def to_markdown(self) -> str:
        """Render the structured result through the public template.

        A result with nothing at all in it renders as the empty string, which is what a
        a query over an empty memory has always returned. A `find` never lands there, since
        its receipt alone is worth printing.
        """
        if not (self.evidence or self.web or self.receipt):
            return ""
        return (await _template.render_async(result=self)).strip()
