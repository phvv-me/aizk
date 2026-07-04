from collections.abc import Iterable
from enum import StrEnum

from ..config import settings


class EntityType(StrEnum):
    """The closed vocabulary of node kinds a fact can point at, the determinism lever that keeps
    the same text yielding the same graph across runs.

    Pydantic renders this enum as a json-schema enum natively, so `ExtractedEntity` annotates its
    `type` field with this class directly and the endpoint's grammar-constrained decoding keeps
    every value inside it. Every member but `RAPTOR_SUMMARY` and `OBSERVATION` is one the extractor
    may emit. Those two are system-written instead, by the RAPTOR tree and the reflective insight
    pass, so `structural` tells the two groups apart and `extractable` renders only the
    extractor-emittable ones for the prompt.
    """

    PAPER = "Paper"
    AUTHOR = "Author"
    THEOREM = "Theorem"
    LEMMA = "Lemma"
    DEFINITION = "Definition"
    PROOF = "Proof"
    CLAIM = "Claim"
    HYPOTHESIS = "Hypothesis"
    METHOD = "Method"
    MODEL = "Model"
    DATASET = "Dataset"
    BENCHMARK = "Benchmark"
    METRIC = "Metric"
    RESULT = "Result"
    HYPERPARAMETER = "Hyperparameter"
    EXPERIMENT = "Experiment"
    EQUATION = "Equation"
    CODE_ARTIFACT = "CodeArtifact"
    CONCEPT = "Concept"
    PROJECT = "Project"
    TOOL = "Tool"
    # coding memory: the things a software session decides, reuses, trips over, and edits
    DECISION = "Decision"
    PATTERN = "Pattern"
    GOTCHA = "Gotcha"
    MODULE = "Module"
    FUNCTION = "Function"
    # structural, system-written types the extractor never emits, see the class docstring
    RAPTOR_SUMMARY = "RaptorSummary"
    OBSERVATION = "Observation"

    @property
    def structural(self) -> bool:
        """Whether this is a system-written type the extractor must never emit."""
        return self in {EntityType.RAPTOR_SUMMARY, EntityType.OBSERVATION}

    @classmethod
    def extractable(cls) -> list[EntityType]:
        """The extraction vocabulary, sorted for a byte-stable, reproducible prompt."""
        return sorted(member for member in cls if not member.structural)


class RelationType(StrEnum):
    """The closed vocabulary of predicates a fact may assert, mirroring `EntityType`'s role for
    the graph's edges.

    Pydantic renders this enum natively, so `TimedFact` and the combined call's wire-format
    `LLMFact` both annotate their `predicate` field with this class directly and the endpoint's
    grammar-constrained decoding keeps every value inside it. Every member but `OBSERVES` is one
    the extractor may emit. `OBSERVES` is the predicate
    every system-written fact carries instead, the reflective insight pass's own derived
    observations and `extract.journal`'s deterministic dated-line parse alike, structural like
    `EntityType.RAPTOR_SUMMARY`/`OBSERVATION`.
    """

    PROVES = "proves"
    REFUTES = "refutes"
    CITES = "cites"
    EXTENDS = "extends"
    USES = "uses"
    EVALUATES_ON = "evaluates_on"
    IMPROVES_OVER = "improves_over"
    DEPENDS_ON = "depends_on"
    CONTRADICTS = "contradicts"
    REPRODUCES = "reproduces"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    PART_OF = "part_of"
    AUTHORED_BY = "authored_by"
    RELATED_TO = "related_to"
    # coding memory: why a decision holds, what a pattern steers clear of, what code does
    BECAUSE = "because"
    AVOIDS = "avoids"
    IMPLEMENTS = "implements"
    FIXES = "fixes"
    # structural, system-written predicate the extractor never emits, see the class docstring
    OBSERVES = "observes"

    @property
    def structural(self) -> bool:
        """Whether this is the system-written predicate the extractor must never emit."""
        return self is RelationType.OBSERVES

    @classmethod
    def extractable(cls) -> list[RelationType]:
        """The extraction vocabulary, sorted for a byte-stable, reproducible prompt."""
        return sorted(member for member in cls if not member.structural)


def check_in_sql(column: str, members: Iterable[str]) -> str:
    """A `column IN (...)` SQL expression over a closed vocabulary's string values.

    Shared by the 0001 migration's CHECK constraint DDL and each model's matching
    `CheckConstraint`, so the database wall and the ORM wall carry the identical expression and
    the alembic autogenerate drift probe never flags one side as different from the other.

    column: column name the constraint restricts.
    members: string values the column may hold, every member of `EntityType` or `RelationType`.
    """
    values = ", ".join(f"'{member}'" for member in members)
    return f"{column} IN ({values})"


# compact prompt fragment that lists the allowed types for the extractor. Sorted so the prompt
# text is byte-stable across processes, which keeps temperature-0 extraction reproducible. The
# template itself lives at settings.ontology_prompt_template, so a deployment may reword the
# rules without touching code.
ONTOLOGY_PROMPT: str = settings.ontology_prompt_template.format(
    entity_count=len(EntityType.extractable()),
    entity_types=", ".join(EntityType.extractable()),
    relation_count=len(RelationType.extractable()),
    relation_types=", ".join(RelationType.extractable()),
)
