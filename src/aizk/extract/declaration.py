import re
from datetime import UTC, datetime

from inflection import underscore
from patos import FrozenModel

from ..ontology import Ontology, System
from .models import ExtractedEntity, Extraction, TimedFact

_TITLE = re.compile(r"^# (?P<title>.+)$", re.MULTILINE)
_TYPE = re.compile(r"^- Type\s+(?P<kind>\S(?:.*\S)?)\s*$", re.IGNORECASE | re.MULTILINE)
_RELATION = re.compile(
    r"^- (?P<predicate>[A-Za-z][\w -]*)\s+\[(?P<kind>[^\]]+)\]\s+(?P<object>\S(?:.*\S)?)\s*$",
    re.MULTILINE,
)
_TAG = re.compile(
    r"^#(?P<kind>[A-Za-z][\w -]*):\s*(?P<object>\S(?:.*\S)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_JOURNAL = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})(?:\s*\([^)]*\))?:\s*(.+)$", re.MULTILINE)


def clean_name(value: str) -> str:
    """Strip Markdown wiki-link decoration while preserving the canonical label."""
    value = value.strip()
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2]
    return value.split("|", 1)[0].strip()


class SourceDeclaration(FrozenModel):
    """Explicit ontology declaration embedded in self-describing Markdown."""

    class Tag(FrozenModel):
        """One generic source association whose key names a live entity kind."""

        object_type: str
        object_name: str
        quote: str

    class Relation(FrozenModel):
        """One declared predicate and typed object entity."""

        predicate: str
        object_type: str
        object_name: str
        quote: str

    title: str | None = None
    subject_type: str | None = None
    tags: tuple[Tag, ...] = ()
    relations: tuple[Relation, ...] = ()

    @classmethod
    def from_text(cls, text: str, title: str | None = None) -> SourceDeclaration:
        """Parse the heading, optional subject type, and generic relation lines."""
        heading = _TITLE.search(text)
        declared_title = heading["title"].strip().rstrip("#").strip() if heading else title
        tags = tuple(
            cls.Tag(
                object_type=match["kind"].strip(),
                object_name=clean_name(match["object"]),
                quote=match.group(0),
            )
            for match in _TAG.finditer(text)
        )
        type_match = _TYPE.search(text)
        explicit_type = type_match["kind"].strip() if type_match else None
        self_types = {
            underscore(tag.object_type)
            for tag in tags
            if declared_title is not None
            and tag.object_name.casefold() == declared_title.casefold()
        }
        if explicit_type is not None:
            self_types.add(underscore(explicit_type))
        if len(self_types) > 1:
            raise ValueError("source title has conflicting ontology kinds")
        subject_type = explicit_type or (next(iter(self_types)) if self_types else None)
        if subject_type is not None and not declared_title:
            raise ValueError("typed source text needs a level-one Markdown title")
        relations = (
            tuple(
                cls.Relation(
                    predicate=underscore(match["predicate"]),
                    object_type=match["kind"].strip(),
                    object_name=clean_name(match["object"]),
                    quote=match.group(0),
                )
                for match in _RELATION.finditer(text)
            )
            if subject_type is not None
            else ()
        )
        return cls(
            title=declared_title,
            subject_type=subject_type,
            tags=tags,
            relations=relations,
        )

    def canonical(self, ontology: Ontology) -> SourceDeclaration:
        """Resolve every declared type and predicate against the live ontology."""
        if self.subject_type is None and not self.tags:
            return self
        return self.model_copy(
            update={
                "subject_type": (
                    ontology.entity_kind(self.subject_type)
                    if self.subject_type is not None
                    else None
                ),
                "tags": tuple(
                    tag.model_copy(update={"object_type": ontology.entity_kind(tag.object_type)})
                    for tag in self.tags
                ),
                "relations": tuple(
                    relation.model_copy(
                        update={
                            "predicate": ontology.relation_kind(relation.predicate),
                            "object_type": ontology.entity_kind(relation.object_type),
                        }
                    )
                    for relation in self.relations
                ),
            }
        )

    def extraction(
        self,
        ontology: Ontology,
        observed_at: datetime,
        expires_at: datetime | None,
    ) -> Extraction:
        """Project this declaration into typed entities and source-grounded facts."""
        declared = self.canonical(ontology)
        if declared.title is None or (declared.subject_type is None and not declared.tags):
            return Extraction(entities=[], facts=[])
        entities = [
            ExtractedEntity(
                name=declared.title,
                type=declared.subject_type or System.Entity.CONCEPT,
            )
        ]
        facts = []
        for tag in declared.tags:
            if tag.object_name.casefold() == declared.title.casefold():
                continue
            entities.append(ExtractedEntity(name=tag.object_name, type=tag.object_type))
            facts.append(
                TimedFact(
                    subject=declared.title,
                    predicate=System.Relation.RELATED_TO,
                    object=tag.object_name,
                    statement=f"{declared.title} relates to {tag.object_name}.",
                    quote=tag.quote,
                    valid_from=observed_at,
                    valid_to=expires_at,
                )
            )
        for relation in declared.relations:
            entities.append(ExtractedEntity(name=relation.object_name, type=relation.object_type))
            words = relation.predicate.replace("_", " ")
            facts.append(
                TimedFact(
                    subject=declared.title,
                    predicate=relation.predicate,
                    object=relation.object_name,
                    statement=f"{declared.title} {words} {relation.object_name}.",
                    quote=relation.quote,
                    valid_from=observed_at,
                    valid_to=expires_at,
                )
            )
        return Extraction(entities=entities, facts=facts)


def journal_facts(text: str, title: str) -> list[TimedFact]:
    """Parse dated journal lines into deterministic observation facts."""
    return [
        TimedFact(
            subject=title,
            predicate=System.Relation.OBSERVES,
            statement=statement.strip(),
            valid_from=datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=UTC),
        )
        for date_text, statement in _JOURNAL.findall(text)
    ]
