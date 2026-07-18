from collections.abc import Mapping, Sequence
from enum import StrEnum, auto
from functools import cached_property
from typing import ClassVar, Self

from patos import FrozenModel
from pydantic import UUID5, Field

from ..templates import environment
from .candidate import Candidate
from .lane import Lane

_template = environment.get_template("recall.md.j2")


class _Provenance(StrEnum):
    """Public evidence provenance independent of internal retrieval lanes."""

    SOURCE = auto()
    DERIVED = auto()
    SESSION = auto()


class _Scope(FrozenModel):
    """One exact Logto scope represented in recalled evidence."""

    name: str
    description: str | None = None


class _Evidence(FrozenModel):
    """One merit-ordered evidence item with exact scope provenance."""

    provenance: _Provenance
    text: str
    scopes: tuple[_Scope, ...] = ()
    resource_uri: str | None = None


class RecallResult(FrozenModel):
    """Structured recall result that can be serialized as JSON or rendered as Markdown."""

    Provenance: ClassVar[type[_Provenance]] = _Provenance
    Scope: ClassVar[type[_Scope]] = _Scope
    Evidence: ClassVar[type[_Evidence]] = _Evidence

    evidence: tuple[_Evidence, ...] = Field(default=(), description="merit-ordered evidence")

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
                        cls.Provenance.SOURCE
                        if candidate.lane is Lane.Kind.SOURCES
                        else cls.Provenance.SESSION
                        if candidate.lane is Lane.Kind.WORKING_MEMORY
                        else cls.Provenance.DERIVED
                    ),
                    text=candidate.line,
                    resource_uri=(
                        f"aizk://artifacts/{candidate.artifact_id}/contents/"
                        f"{candidate.artifact_content_id}"
                        if candidate.artifact_id is not None
                        and candidate.artifact_content_id is not None
                        else None
                    ),
                    scopes=(
                        tuple(
                            sorted(
                                (scopes[scope] for scope in candidate.scopes),
                                key=lambda scope: scope.name,
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
        """Render the structured result through the public recall template."""
        return (await _template.render_async(result=self)).strip() if self.evidence else ""
